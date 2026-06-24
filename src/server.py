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
import asyncio

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

import config
from rizom_connection import RizomUVConnection, RizomUVError


# MCP 서버 인스턴스
app = Server("rizomuv-mcp")

# RizomUV 연결 (import 시점에 RizomUV 설치 여부와 무관하게 생성 가능)
rizom = RizomUVConnection()


@app.list_tools()
async def list_tools() -> list[Tool]:
    """사용 가능한 도구 목록."""
    file_path_schema = {
        "type": "object",
        "properties": {
            "input_path": {"type": "string", "description": "입력 메시 파일 절대 경로 (.fbx/.obj)"},
            "output_path": {"type": "string", "description": "출력 메시 파일 절대 경로 (.fbx/.obj)"},
        },
        "required": ["input_path", "output_path"],
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
                },
            },
        ),
        Tool(
            name="unfold_uvs",
            description="선택된 UV 를 언폴드",
            inputSchema={"type": "object", "properties": {}},
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
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="weld_uvs",
            description="UV 공간에서 겹치는 에지를 다시 붙임 (심 병합 — Cut 의 반대)",
            inputSchema={"type": "object", "properties": {}},
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
                },
                "required": ["output_path"],
            },
        ),
        Tool(
            name="get_info",
            description="연결된 RizomUV 버전 + 설정 스냅샷 (라이브 세션)",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="execute_command",
            description="라이브 세션에서 임의의 RizomUV 명령을 이름+파라미터로 실행 (전체 API 범용 해치). 파라미터 스키마는 RizomUV 설치폴더 doc/index.html 참조",
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "RizomUV 명령(태스크) 이름 — 예: Unfold/Pack/Optimize/Weld/IslandGroups/SymmetrySet/Set/Get"},
                    "parameters": {"type": "object", "description": "명령 파라미터 테이블 (doc/index.html 스키마)"},
                },
                "required": ["command"],
            },
        ),
        Tool(
            name="close_session",
            description="RizomUV 라이브 세션(창)을 닫음 — 다음 작업 시 자동으로 다시 열림",
            inputSchema={"type": "object", "properties": {}},
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


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """도구 실행. RizomUVError 는 친절한 JSON 에러로 변환 (크래시 X)."""
    try:
        if name == "check_connection":
            status = rizom.check_connection()
            status["config"] = config.as_dict()
            return _json(status)

        if name == "load_mesh":
            return _json(rizom.load_mesh(
                arguments["input_path"],
                import_groups=arguments.get("import_groups", True),
            ))

        if name == "cut_by_sharp_edges":
            return _json(rizom.cut_by_sharp_edges(angle=arguments.get("angle", 45.0)))

        if name == "select_primitives":
            return _json(rizom.select_primitives(
                mode=arguments.get("mode", "Island"),
                select_all=arguments.get("select_all", True),
            ))

        if name == "unfold_uvs":
            return _json(rizom.unfold_uvs())

        if name == "pack_uvs":
            return _json(rizom.pack_uvs(
                translate=arguments.get("translate", True),
                map_resolution=arguments.get("map_resolution"),
                texel_density=arguments.get("texel_density"),
                rotate=arguments.get("rotate", True),
            ))

        if name == "save_mesh":
            return _json(rizom.save_mesh(arguments["output_path"]))

        if name == "unwrap_file":
            return _json(rizom.unwrap_file(
                arguments["input_path"],
                arguments["output_path"],
                cut_angle=arguments.get("cut_angle", 45.0),
                import_groups=arguments.get("import_groups", True),
                pack=arguments.get("pack", True),
            ))

        if name == "optimize_uvs":
            return _json(rizom.optimize_uvs())

        if name == "weld_uvs":
            return _json(rizom.weld_uvs())

        if name == "export_uv_layout":
            return _json(rizom.export_uv_layout(
                arguments["output_path"],
                width=arguments.get("width", 1024),
                height=arguments.get("height", 1024),
            ))

        if name == "get_info":
            return _json(rizom.get_info())

        if name == "execute_command":
            return _json(rizom.execute_command(
                arguments["command"],
                arguments.get("parameters"),
            ))

        if name == "close_session":
            return _json(rizom.close_session())

        if name == "execute_lua":
            return _json(rizom.execute_lua(arguments["script"]))

        return _json({"ok": False, "error": f"Unknown tool: {name}"})

    except RizomUVError as e:
        return _json({"ok": False, "error": str(e)})
    except KeyError as e:
        return _json({"ok": False, "error": f"Missing required argument: {e}"})
    except Exception as e:  # noqa: BLE001 - 도구는 절대 서버를 죽이면 안 됨
        return _json({"ok": False, "error": f"{type(e).__name__}: {e}"})


async def _amain():
    """MCP 서버 비동기 진입점 (stdio transport)."""
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def main():
    """MCP 서버 시작."""
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
