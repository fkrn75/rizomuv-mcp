"""
RizomUV MCP Server
Claude 가 RizomUV (UV 언랩 자동화) 를 제어할 수 있게 해주는 MCP 서버.

아키텍처 (팀리드 확정): SubstancePainterMCP 와 동일하게
저수준 `mcp.server.Server` + stdio transport + @app.list_tools()/@app.call_tool()
데코레이터 패턴을 사용한다 (FastMCP 아님).

자동화 방식 (Scout 확정, DUAL PATH):
  Path A — RizomUVLink (번들 파이썬 모듈, ZeroMQ). v2022.2+ PRIMARY.
  Path B — CLI + Lua 스크립트 (`rizomuv.exe -cfi script.lua`). 구버전/headless 폴백.
연결 클래스 RizomUVConnection 이 두 경로를 자동 선택한다.

핵심: RizomUV 미설치 환경에서도 import / 도구 등록 / check_connection 이 깨지지 않는다.
"""

import json
import atexit
import asyncio

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

import config
from rizom_connection import RizomUVConnection, RizomUVError
from rizom_pool import RizomUVPool, DEFAULT_SESSION


# MCP 서버 인스턴스
app = Server("rizomuv-mcp")

# RizomUV 인스턴스 풀 (lazy — import/생성 시점에 어떤 프로세스도 띄우지 않음).
# 여러 서브에이전트가 서로 다른 인스턴스(독립 ZMQ 포트)를 잡으면 진짜 병렬 실행된다.
pool = RizomUVPool()

# 인스턴스를 점유하지 않는 도구(연결 점검 / Path B 배치)는 풀을 거치지 않고
# 가벼운 throwaway 연결로 처리한다 — 인스턴스 0개·RizomUV 미설치에서도 안전.
_probe = RizomUVConnection()

# ※ 전역 _tool_lock 제거됨: 직렬화는 더 이상 전 서버가 아니라 per-instance op_lock 으로만.
#    서로 다른 인스턴스의 작업은 asyncio.to_thread 로 진짜 병렬 실행된다.


# 프로세스 종료 시 모든 RizomUV 라이브 세션을 닫아 좀비 인스턴스를 방지한다.
# atexit 은 await 불가 → pool.close_all() 은 동기(conn.quit() 직접 호출, 실패 무시).
@atexit.register
def _cleanup_on_exit():
    try:
        pool.close_all()
    except Exception:
        pass


@app.list_tools()
async def list_tools() -> list[Tool]:
    """사용 가능한 도구 목록."""
    # stateful 도구는 선택적 `session` 으로 인스턴스 affinity 를 지정할 수 있다.
    # 같은 session 문자열을 주는 호출들은 같은 RizomUV 라이브 세션(인스턴스)에 묶이고,
    # 생략하면 레거시 호환 단일 공유 세션("__default__")으로 동작한다.
    # 서로 다른 에이전트가 서로 다른 session 을 주면 독립 인스턴스에서 병렬 실행된다.
    session_prop = {
        "session": {
            "type": "string",
            "description": "세션 ID(선택) — 같은 ID 의 호출은 같은 RizomUV 인스턴스에 묶임. "
                           "생략 시 공유 기본 세션. 끝나면 close_session 으로 닫아 인스턴스 반환.",
        }
    }
    return [
        Tool(
            name="check_connection",
            description="RizomUV 설치/연결 상태 확인 (미설치여도 안전). 사용 가능한 경로(A/B)와 설정 스냅샷 반환",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="load_mesh",
            description="메시 파일(.fbx/.obj)을 RizomUV 로 로드",
            inputSchema={
                "type": "object",
                "properties": {
                    "input_path": {"type": "string", "description": "입력 메시 파일 절대 경로"},
                    "import_groups": {"type": "boolean", "description": "그룹 정보 유지 (기본 true)"},
                    **session_prop,
                },
                "required": ["input_path"],
            },
        ),
        Tool(
            name="cut_by_sharp_edges",
            description="샤프 에지(각도 임계값) 기준으로 UV 심 자동 컷",
            inputSchema={
                "type": "object",
                "properties": {
                    "angle": {"type": "number", "description": "컷 각도 임계값 (도, 기본 45)"},
                    **session_prop,
                },
            },
        ),
        Tool(
            name="select_primitives",
            description="프리미티브(Island/Polygon 등) 선택",
            inputSchema={
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "description": "프리미티브 타입 (기본 Island)"},
                    "select_all": {"type": "boolean", "description": "전체 선택 (기본 true)"},
                    **session_prop,
                },
            },
        ),
        Tool(
            name="unfold_uvs",
            description="선택된 UV 를 언폴드",
            inputSchema={"type": "object", "properties": {**session_prop}},
        ),
        Tool(
            name="pack_uvs",
            description="UV 아일랜드를 패킹 (맵 해상도·텍셀 밀도·회전 옵션)",
            inputSchema={
                "type": "object",
                "properties": {
                    "translate": {"type": "boolean", "description": "이동 허용(메인 패킹 알고리즘, 기본 true)"},
                    "map_resolution": {"type": "integer", "description": "최종 텍스처 맵 해상도 px (예 1024/2048/4096)"},
                    "texel_density": {"type": "number", "description": "모든 아일랜드 목표 텍셀 밀도"},
                    "rotate": {"type": "boolean", "description": "패킹 시 아일랜드 회전 허용 (기본 true)"},
                    **session_prop,
                },
            },
        ),
        Tool(
            name="save_mesh",
            description="현재 메시를 파일(.fbx/.obj)로 저장",
            inputSchema={
                "type": "object",
                "properties": {
                    "output_path": {"type": "string", "description": "출력 메시 파일 절대 경로"},
                    **session_prop,
                },
                "required": ["output_path"],
            },
        ),
        Tool(
            name="unwrap_file",
            description="전체 UV 언랩 파이프라인 (Load→Cut→Unfold→Pack→Save) 을 한 번에 실행",
            inputSchema={
                "type": "object",
                "properties": {
                    "input_path": {"type": "string", "description": "입력 메시 파일 절대 경로 (.fbx/.obj)"},
                    "output_path": {"type": "string", "description": "출력 메시 파일 절대 경로 (.fbx/.obj)"},
                    "cut_angle": {"type": "number", "description": "샤프 에지 컷 각도 (기본 45)"},
                    "import_groups": {"type": "boolean", "description": "그룹 정보 유지 (기본 true)"},
                    "pack": {"type": "boolean", "description": "패킹 수행 (기본 true)"},
                },
                "required": ["input_path", "output_path"],
            },
        ),
        Tool(
            name="optimize_uvs",
            description="언폴드 결과를 추가 최적화 (스트레칭/왜곡 감소)",
            inputSchema={"type": "object", "properties": {**session_prop}},
        ),
        Tool(
            name="weld_uvs",
            description="UV 공간에서 겹치는 에지를 다시 붙임 (심 병합 — Cut 의 반대)",
            inputSchema={"type": "object", "properties": {**session_prop}},
        ),
        Tool(
            name="export_uv_layout",
            description="현재 UV 레이아웃을 이미지 파일(PNG/TIFF)로 래스터 익스포트 (베이크)",
            inputSchema={
                "type": "object",
                "properties": {
                    "output_path": {"type": "string", "description": "출력 이미지 절대 경로 (.png/.tif 등)"},
                    "width": {"type": "integer", "description": "이미지 너비 px (기본 1024)"},
                    "height": {"type": "integer", "description": "이미지 높이 px (기본 1024)"},
                    **session_prop,
                },
                "required": ["output_path"],
            },
        ),
        Tool(
            name="get_info",
            description="연결된 RizomUV 버전 + 설정 스냅샷 + 인스턴스 풀 상태. session 지정 시 그 세션 인스턴스의 라이브 버전을 함께 반환",
            inputSchema={"type": "object", "properties": {**session_prop}},
        ),
        Tool(
            name="execute_command",
            description="라이브 세션에서 임의의 RizomUV 명령을 이름+파라미터로 실행 (전체 API 범용 해치). 파라미터 스키마는 RizomUV 설치폴더 doc/index.html 참조",
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "RizomUV 명령(태스크) 이름 — 예: Unfold/Pack/Optimize/Weld/IslandGroups/SymmetrySet/Set/Get"},
                    "parameters": {"type": "object", "description": "명령 파라미터 테이블 (doc/index.html 스키마)"},
                    **session_prop,
                },
                "required": ["command"],
            },
        ),
        Tool(
            name="close_session",
            description="RizomUV 라이브 세션(창)을 닫고 인스턴스를 풀에 반환 — 다음 작업 시 자동으로 다시 열림. session 지정 시 해당 세션만 닫음",
            inputSchema={"type": "object", "properties": {**session_prop}},
        ),
        Tool(
            name="execute_lua",
            description="임의의 RizomUV Lua 스크립트를 배치(CLI -cfi)로 실행 — 별도 인스턴스 생성. 라이브 세션 제어는 execute_command 권장",
            inputSchema={
                "type": "object",
                "properties": {
                    "script": {"type": "string", "description": "실행할 Lua 스크립트 (끝에 ZomQuit() 권장)"},
                },
                "required": ["script"],
            },
        ),
    ]


def _json(payload) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, indent=2))]


# 워커를 점유하는 stateful 도구: 도구이름 → (RizomUVConnection 메서드명, kwargs).
# 콜러블은 프로세스 경계를 못 넘으므로 '이름+인자'로 워커에 보내 그 메서드를 실행시킨다.
def _stateful_args(name: str, arguments: dict):
    """stateful 도구를 (메서드명, kwargs) 로 변환. 미지원이면 None."""
    if name == "load_mesh":
        return "load_mesh", {"input_path": arguments["input_path"],
                             "import_groups": arguments.get("import_groups", True)}
    if name == "cut_by_sharp_edges":
        return "cut_by_sharp_edges", {"angle": arguments.get("angle", 45.0)}
    if name == "select_primitives":
        return "select_primitives", {"mode": arguments.get("mode", "Island"),
                                     "select_all": arguments.get("select_all", True)}
    if name == "unfold_uvs":
        return "unfold_uvs", {}
    if name == "pack_uvs":
        return "pack_uvs", {"translate": arguments.get("translate", True),
                            "map_resolution": arguments.get("map_resolution"),
                            "texel_density": arguments.get("texel_density"),
                            "rotate": arguments.get("rotate", True)}
    if name == "save_mesh":
        return "save_mesh", {"output_path": arguments["output_path"]}
    if name == "optimize_uvs":
        return "optimize_uvs", {}
    if name == "weld_uvs":
        return "weld_uvs", {}
    if name == "export_uv_layout":
        return "export_uv_layout", {"output_path": arguments["output_path"],
                                    "width": arguments.get("width", 1024),
                                    "height": arguments.get("height", 1024)}
    if name == "execute_command":
        return "execute_command", {"command": arguments["command"],
                                   "parameters": arguments.get("parameters")}
    return None


async def _session_call(session: str, method: str, **kwargs):
    """세션 핀 워커에서 메서드 1개 실행.

    워커가 작업 도중 죽으면(프로세스 사망) 그 워커를 풀에서 퇴출하고 permit 을 반납한 뒤
    예외를 전파한다 → 세션 핀 경로에서의 permit 누수/죽은 워커 재사용을 방지(Reviewer2 지적 #2·#3).
    워커가 살아있는 정상 작업 오류는 세션을 유지(퇴출 안 함).
    """
    inst = await pool.acquire(session_id=session)
    try:
        return await inst.call(method, **kwargs)
    except Exception:
        if not inst.alive():
            await pool.discard(inst, session)
        raise


async def _call_tool_impl(name: str, arguments: dict) -> list[TextContent]:
    """도구 실행 본체. RizomUVError 는 친절한 JSON 에러로 변환 (크래시 X).

    라우팅 3종:
      (1) no-instance — 인스턴스/풀 불필요. _probe(throwaway 연결)로 처리.
          check_connection / execute_lua. 인스턴스 0개·RizomUV 미설치에서도 안전.
      (2) ephemeral atomic — unwrap_file. 세션 없이 acquire→run→release(try/finally).
          서로 다른 호출이 서로 다른 인스턴스를 잡아 진짜 N-way 병렬.
      (3) stateful session — 나머지. session(생략 시 "__default__")으로 인스턴스 핀.
          release 하지 않음(핀 유지). close_session 으로만 반환.
    """
    try:
        # ---- (1) no-instance: 풀 우회 ----
        if name == "check_connection":
            status = _probe.check_connection()
            status["config"] = config.as_dict()
            status["pool"] = pool.status()
            return _json(status)

        if name == "execute_lua":
            # Path B(CLI 배치) — 별도 인스턴스 생성, 풀/소켓 불필요.
            return _json(await asyncio.to_thread(_probe.execute_lua, arguments["script"]))

        # ---- (2) ephemeral atomic: unwrap_file ----
        if name == "unwrap_file":
            inst = await pool.acquire()  # session 없음 → ephemeral (서로 다른 호출 = 다른 워커 = 병렬)
            try:
                result = await inst.call(
                    "unwrap_file",
                    input_path=arguments["input_path"],
                    output_path=arguments["output_path"],
                    cut_angle=arguments.get("cut_angle", 45.0),
                    import_groups=arguments.get("import_groups", True),
                    pack=arguments.get("pack", True),
                )
            finally:
                await pool.release(inst)  # 예외든 정상이든 반드시 permit 반납
            return _json(result)

        # ---- (3) stateful: session affinity (기본 "__default__") ----
        session = arguments.get("session") or DEFAULT_SESSION

        if name == "get_info":
            # 풀 상태는 항상 포함. 라이브 버전은 그 세션 워커에서 조회.
            info = {"ok": True, "pool": pool.status(), "config": config.as_dict()}
            info["live"] = await _session_call(session, "get_info")
            return _json(info)

        if name == "close_session":
            return _json(await pool.close_session(session))

        sm = _stateful_args(name, arguments)
        if sm is not None:
            # 핀된 세션이므로 정상 시엔 release 안 함(close_session 까지 유지).
            # 워커 사망 시엔 _session_call 이 퇴출 + permit 반납.
            return _json(await _session_call(session, sm[0], **sm[1]))

        return _json({"ok": False, "error": f"Unknown tool: {name}"})

    except RizomUVError as e:
        return _json({"ok": False, "error": str(e)})
    except KeyError as e:
        return _json({"ok": False, "error": f"Missing required argument: {e}"})
    except Exception as e:  # noqa: BLE001 - 도구는 절대 서버를 죽이면 안 됨
        return _json({"ok": False, "error": f"{type(e).__name__}: {e}"})


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """도구 호출 진입점.

    전역 _tool_lock 제거 — 더 이상 모든 호출을 직렬화하지 않는다.
    동시성은 풀이 관리한다(per-instance op_lock + asyncio.to_thread → 인스턴스 간 병렬).
    """
    return await _call_tool_impl(name, arguments)


async def _amain():
    """MCP 서버 비동기 진입점 (stdio transport)."""
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def main():
    """MCP 서버 시작."""
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
