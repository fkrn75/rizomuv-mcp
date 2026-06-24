# RizomUV MCP Server

> **RizomUV(UV 언랩 툴)를 Claude에서 자연어로 제어하는 MCP 서버.** RizomUVLink(ZMQ) 라이브 소켓으로 실행 중인 RizomUV에 직접 붙어 로드 → 심 컷 → 언폴드 → 패킹 → 저장을 자동화합니다.
> `execute_command` 로 RizomUV 전체 태스크 API에 접근하고, 구버전은 `-cfi` Lua 배치로 폴백하며, 서브에이전트 동시 사용까지 안전하게 설계됐습니다. (사실상 첫 RizomUV MCP)

저수준 `mcp.server.Server` + stdio transport 패턴 (SubstancePainterMCP 와 동일).
RizomUV 가 설치되어 있지 않아도 서버 import / 도구 등록 / 헬스 체크가 깨지지 않습니다.

## 자동화 방식 (DUAL PATH)

| 경로 | 방식 | 대상 |
|------|------|------|
| **Path A (권장)** | **RizomUVLink** — RizomUV 에 번들된 파이썬 모듈, ZeroMQ/TCP 소켓 | RizomUV 2022.2+ |
| **Path B (폴백)** | **CLI + Lua** — `rizomuv.exe -cfi script.lua` | 구버전 / headless |

연결 클래스 `RizomUVConnection` 이 설치 버전을 감지해 경로를 자동 선택합니다.
`rizomuvlink` 모듈은 RizomUV 설치 폴더에 있으므로 **런타임에 lazy import** 됩니다.

## 요구사항

- **RizomUV 2022.2+** (Path A 라이브 소켓) 또는 구버전 (Path B 배치)
- **Python 3.10** — ⚠️ Path A 필수. RizomUVLink는 컴파일 `.pyd`를 **Python 3.6~3.10용만** 제공하고 `mcp`는 **≥3.10**을 요구하므로, 교집합인 **정확히 3.10**으로 venv를 만들어야 라이브 소켓이 켜진다. 3.11+ venv면 RizomUVLink import가 불가해 Path B(배치)만 동작.
- 의존성: `mcp>=1.0.0`, `pyzmq>=24.0.0`

> **검증 상태**: RizomUV 2023.0 + Python 3.10 venv에서 `unwrap_file` 풀 파이프라인(Load→Select 샤프에지→Cut→Unfold→Pack→Save) **라이브 엔드투엔드 동작 확인**. 도구 파라미터는 설치폴더 `doc/index.html`(공식 API 레퍼런스) 기준으로 검증됨(샤프에지 선택 = `Select` 의 `Auto.SharpEdges.AngleMin`).

## 설치

### 1. 가상환경 + 의존성

```powershell
cd C:\Users\hong\rizomuv-mcp
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. 환경 변수 (선택 — 전부 윈도우 기본값/자동탐지 있음)

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `RIZOMUV_HOME` | 레지스트리 자동 탐지 | RizomUV **설치 디렉터리** (rizomuvlink/rizomuv.exe 위치) |
| `RIZOMUV_EXE` | `RIZOMUV_HOME\rizomuv.exe` | `rizomuv.exe` 절대 경로 (HOME 보다 우선) |
| `RIZOMUV_HOST` | `127.0.0.1` | 라이브 소켓 호스트 |
| `RIZOMUV_PORT` | `0` (자동 할당) | 라이브 소켓 포트 |
| `RIZOMUV_TIMEOUT` | `600` | 실행 타임아웃(초) |

설치 경로 탐지 순서: `RIZOMUV_EXE` → `RIZOMUV_HOME` → 레지스트리 `HKLM\SOFTWARE\Rizom Lab`(최신 버전 우선) → Program Files 후보.

### 3. Claude Desktop 등록

`%APPDATA%\Claude\claude_desktop_config.json` 에 추가:

```json
{
  "mcpServers": {
    "rizomuv": {
      "command": "C:\\Users\\hong\\rizomuv-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\Users\\hong\\rizomuv-mcp\\src\\server.py"],
      "cwd": "C:\\Users\\hong\\rizomuv-mcp\\src",
      "env": {
        "RIZOMUV_HOME": "C:\\Program Files\\Rizom Lab\\RizomUV 2023.0"
      }
    }
  }
}
```

> `env` 블록은 선택사항입니다 (레지스트리 자동 탐지 시 생략 가능).
> 설정 후 Claude Desktop 을 완전히 종료했다 다시 실행하세요.

## 도구 (15종)

| 도구 | 설명 |
|------|------|
| `check_connection()` | 설치/연결 상태 + 사용 가능 경로(A/B) + 설정 스냅샷 |
| `get_info()` | 연결된 RizomUV 버전 + 설정 스냅샷 (라이브) |
| `load_mesh(input_path, import_groups=true)` | 메시 파일(.fbx/.obj) 로드 |
| `cut_by_sharp_edges(angle=45)` | 샤프 에지(법선 각도) 기준 심 자동 선택+컷 |
| `select_primitives(mode="Island", select_all=true)` | 프리미티브 선택 |
| `unfold_uvs()` | 선택 UV 언폴드 |
| `optimize_uvs()` | 언폴드 결과 추가 최적화 (스트레칭/왜곡 감소) |
| `weld_uvs()` | 겹치는 에지 재결합 (심 병합 — Cut 의 반대) |
| `pack_uvs(translate=true, map_resolution?, texel_density?, rotate=true)` | UV 아일랜드 패킹 (맵 해상도·텍셀 밀도·회전) |
| `save_mesh(output_path)` | 메시를 파일(.fbx/.obj)로 저장 |
| `export_uv_layout(output_path, width=1024, height=1024)` | UV 레이아웃을 이미지(PNG/TIFF)로 익스포트 |
| `unwrap_file(input_path, output_path, cut_angle=45, ...)` | 전체 파이프라인 (Load→Cut→Unfold→Pack→Save) |
| `execute_command(command, parameters?)` | **라이브 세션에서 임의 RizomUV 명령 실행 (전체 API 범용 해치)** |
| `close_session()` | RizomUV 라이브 세션(창) 닫기 — 다음 작업 시 자동 재실행 |
| `execute_lua(script)` | 임의 Lua 스크립트를 배치(CLI `-cfi`)로 실행 |

입출력 포맷: **FBX / OBJ** (+ UV 레이아웃 PNG/TIFF).

> **전체 기능 접근**: `execute_command` 로 `Optimize`/`Weld`/`IslandGroups`/`SymmetrySet`/`Move`/`Deform`/`Set`/`Get` 등 RizomUV 의 전체 태스크 API 에 라이브로 접근할 수 있습니다. 파라미터 스키마는 설치폴더 `doc/index.html`(공식 API 레퍼런스) 참조.

## 서브에이전트 / 동시 사용 안전성

여러 서브에이전트가 **하나의 MCP 서버 프로세스 = 단일 RizomUV 라이브 세션**을 공유합니다. 이를 안전하게 만드는 장치:

- **호출 직렬화** — 모든 도구 호출은 내부 락(`asyncio.Lock`)으로 원자적으로 처리됩니다. 한 에이전트의 작업이 끝나야 다음 호출이 시작되어, 작업 도중 다른 에이전트가 끼어들어 mesh 상태가 섞이지 않습니다.
- **stdout 보호** — RizomUV 실행(`Popen`) 시 자식 프로세스가 MCP 의 stdout(JSON-RPC 채널)을 오염시키지 않도록 fd 를 격리합니다(오염되면 전체 연결이 끊김).
- **cwd 보호** — RizomUVLink 가 바꾸는 작업 디렉터리를 매 실행 후 자동 원복합니다.
- **자동 정리** — 서버 프로세스 종료 시 RizomUV 인스턴스를 닫아 좀비 프로세스/창을 방지합니다(`atexit`).
- **무중단 헬스체크** — `check_connection`/`get_info` 는 RizomUV 를 실행하지 않고 상태만 확인하므로 서브에이전트가 안전하게 가용성을 먼저 점검할 수 있습니다.

**권장 사용 패턴(서브에이전트)**

- 가능하면 **원샷 `unwrap_file`**(Load→Cut→Unfold→Pack→Save 한 호출)을 쓰세요. 자체 완결적이라 매 호출이 `Load` 로 시작해 동시 사용에서도 상태 오염이 없습니다.
- 단계별 도구(`load_mesh` → `unfold_uvs` → …)를 여러 호출로 나누면, 단일 공유 세션 특성상 다른 에이전트 호출이 그 사이에 끼어들 수 있습니다. 멀티에이전트 동시 작업에서는 `unwrap_file` 또는 `execute_command` 로 묶으세요.
- 끝나면 `close_session()` 으로 창을 닫을 수 있습니다(다음 작업 시 자동 재실행).

## 파일 구조

```
rizomuv-mcp/
├── src/
│   ├── server.py           # MCP 서버 엔트리포인트 (mcp.server.Server, stdio)
│   ├── rizom_connection.py # RizomUVConnection — Path A/B 자동 선택
│   └── config.py           # 환경 변수 + 레지스트리 설치 탐지
├── requirements.txt
├── pyproject.toml
├── .gitignore
└── README.md
```

## 문제 해결

### `check_connection` 이 `exe_found: false`

1. RizomUV 설치 여부 확인
2. `RIZOMUV_HOME` 또는 `RIZOMUV_EXE` 로 경로 직접 지정
3. 서버는 미설치 상태에서도 크래시 없이 동작 (graceful) — 도구는 친절한 에러를 반환

### Claude 에서 도구가 안 보임

1. `claude_desktop_config.json` 경로/문법 확인
2. Claude Desktop 완전 재시작 (트레이 아이콘까지 종료)

## 참고

- [MCP Protocol](https://modelcontextprotocol.io/)
- RizomUVLink: RizomUV 번들 모듈 / github.com/RemiArq/RizomUVLink
- Lua API: `ZomLoad / ZomCut / ZomSelect / ZomUnfold / ZomPack / ZomSave / ZomQuit`
