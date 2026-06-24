# RizomUV MCP Server

> **RizomUV(UV 언랩 툴)를 Claude에서 자연어로 제어하는 MCP 서버.** RizomUVLink(ZMQ) 라이브 소켓으로 실행 중인 RizomUV에 직접 붙어 로드 → 심 컷 → 언폴드 → 패킹 → 저장을 자동화합니다.
> `execute_command` 로 RizomUV 전체 태스크 API에 접근하고, 구버전은 `-cfi` Lua 배치로 폴백하며, **인스턴스 풀로 여러 RizomUV를 진짜 병렬 구동**(서브에이전트 동시 작업)하도록 설계됐습니다. (사실상 첫 RizomUV MCP)

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
| `RIZOMUV_POOL_SIZE` | `2` | **인스턴스 풀 크기** — 동시에 띄울 RizomUV.exe 개수(진짜 병렬). `1`이면 단일 세션. 각 인스턴스는 full 프로세스(RAM/GPU 무거움) → 머신 사양에 맞게 조정 |
| `RIZOMUV_ACQUIRE_TIMEOUT` | `120` | 풀이 가득 찼을 때 빈 인스턴스를 기다리는 최대 초 |

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
| `check_connection()` | 설치/연결 상태 + 사용 가능 경로(A/B) + 설정 + **풀 상태** 스냅샷 |
| `get_info(session?)` | 풀 상태 + 설정 + (해당 세션 인스턴스의) 라이브 RizomUV 버전 |
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
| `close_session(session?)` | RizomUV 라이브 세션(창) 닫고 인스턴스를 풀에 반환 — 다음 작업 시 자동 재실행 |
| `execute_lua(script)` | 임의 Lua 스크립트를 배치(CLI `-cfi`)로 실행 |

> **세션 파라미터**: `unwrap_file`·`execute_lua`·`check_connection` 을 제외한 stateful 도구는 선택적 `session` 문자열을 받습니다. 같은 `session` 의 호출은 같은 RizomUV 인스턴스(라이브 세션)에 묶이고, 서로 다른 `session` 은 풀 한도 내에서 **독립 인스턴스에서 병렬** 실행됩니다. `session` 생략 시 공유 기본 세션(`__default__`)을 씁니다.

입출력 포맷: **FBX / OBJ** (+ UV 레이아웃 PNG/TIFF).

> **전체 기능 접근**: `execute_command` 로 `Optimize`/`Weld`/`IslandGroups`/`SymmetrySet`/`Move`/`Deform`/`Set`/`Get` 등 RizomUV 의 전체 태스크 API 에 라이브로 접근할 수 있습니다. 파라미터 스키마는 설치폴더 `doc/index.html`(공식 API 레퍼런스) 참조.

## 서브에이전트 / 동시 사용 — 인스턴스 풀 (멀티프로세스, 진짜 병렬)

이 서버는 **RizomUV 워커 프로세스 풀**을 관리합니다. 최대 `RIZOMUV_POOL_SIZE`(기본 2)개의 **독립 Python 워커 프로세스**(`worker.py`)를 띄우고, 각 워커가 자기만의 RizomUV 인스턴스(자기 RizomUVLink/ZMQ 포트)를 소유합니다. 부모(MCP 서버)는 워커와 **로컬 TCP 소켓 IPC**(길이프리픽스 JSON)로 명령을 주고받습니다.

> **왜 멀티프로세스인가**: RizomUVLink 는 Boost.Python(`.pyd`) 확장이라 호출이 진행되는 동안 **GIL 을 놓지 않습니다.** 그래서 한 파이썬 프로세스 안에서 스레드로 N개 인스턴스를 돌리면 RizomUVLink 호출이 GIL 에 직렬화됩니다(실측: 24코어에서도 2개 동시 **0.9x**). 인스턴스마다 **별도 프로세스**(각자 자기 GIL)로 띄워야 진짜 병렬이 됩니다.
>
> **실측 병렬 효율**(RizomUV 2023.0, 24코어): 멀티프로세스 2워커 동시 처리 speedup ≈ **1.4–1.6x**, 워크로드가 무거울수록 2x 에 수렴(헤비 K=40 에서 par2 가 단일 실행 시간에 근접 = 2번째 작업의 ~80% 가 겹침). 2x 미만인 이유는 per-op IPC/GUI 오버헤드 + **RizomUV 솔버가 TBB 로 내부 멀티스레드**라 한 인스턴스가 이미 여러 코어를 쓰기 때문. (스레드 방식 0.9x 대비 명확한 개선.)

동시성 설계:

- **프로세스 격리** — 각 워커가 별도 프로세스라 `os.chdir`/stdout/GIL 같은 전역 부작용이 자연 격리됩니다(부모 쪽 전역 launch 락 불필요).
- **워커당 직렬, 워커 간 병렬** — 한 워커는 `op_lock` 으로 동시 1요청만 처리하고, 서로 다른 워커는 완전 병렬입니다. 블로킹 IPC 는 `asyncio.to_thread` 로 이벤트 루프 밖에서 대기하며, 실제 계산은 워커 프로세스가 병렬 수행합니다.
- **용량 게이트** — `asyncio.Semaphore(pool_size)` 로 동시 워커 수를 제한하고, 가득 차면 `RIZOMUV_ACQUIRE_TIMEOUT` 후 친절한 에러를 반환합니다.
- **stdout 격리** — 워커는 IPC 를 소켓으로 하고 자기 stdout 을 devnull 로 돌려, RizomUV 출력이 부모의 MCP stdio(JSON-RPC)를 절대 오염시키지 않습니다.
- **자동 정리** — 워커를 닫을 때 `taskkill /T`(트리)로 워커 + 자식 `rizomuv.exe` 를 함께 정리합니다(`close_session`·서버 종료 `atexit` 모두). 부모(MCP 서버)가 갑자기 죽으면 워커가 소켓 EOF 를 감지해 자기 RizomUV 를 정리합니다. 워커가 죽은 채 들어온 다음 호출은 그 워커를 풀에서 퇴출하고 permit 을 반납합니다(누수 방지). (워커 프로세스 자체가 비정상 강제종료되는 드문 경우엔 그 RizomUV 가 best-effort 로 잠깐 남을 수 있습니다.)
- **무중단 헬스체크** — `check_connection` 은 워커를 띄우지 않고 상태(+풀 상태)만 보고합니다.

**세션 모델과 병렬성 — 중요**

- 병렬성을 얻으려면 **(a) ephemeral `unwrap_file`** 을 쓰거나 **(b) 호출마다 서로 다른 `session` 문자열**을 주세요. 서로 다른 세션/ephemeral 호출은 풀 한도까지 독립 인스턴스에서 동시에 돕니다.
- ⚠️ **`session` 없이 호출하는 레거시/단계별 도구는 모두 하나의 공유 인스턴스(`__default__`)에 묶여 직렬 실행되며 병렬성이 없습니다**(하위호환 보장). 멀티에이전트 병렬이 필요하면 ephemeral `unwrap_file` 을 쓰거나 에이전트마다 고유 `session` id 를 부여하세요.
- 단계별 도구(`load_mesh` → `unfold_uvs` → …)를 한 에이전트가 묶어 쓸 때는 동일 `session` 을 넘겨 같은 인스턴스에 고정하고, 끝나면 `close_session(session)` 으로 그 인스턴스를 풀에 반환하세요(생략 시 `__default__` 세션 닫힘).
- 세션을 핀해두고 `close_session` 을 호출하지 않으면 그 인스턴스가 풀 슬롯을 계속 점유합니다 — 장수 세션은 반드시 닫아 슬롯을 회수하세요.

## 파일 구조

```
rizomuv-mcp/
├── src/
│   ├── server.py           # MCP 서버 엔트리포인트 (mcp.server.Server, stdio) — 풀 라우팅
│   ├── rizom_pool.py       # RizomUVPool/RizomUVWorker — N개 워커 프로세스 + 동시성 제어
│   ├── worker.py           # 워커 서브프로세스 — RizomUV 인스턴스 1개 소유, 로컬 소켓 IPC
│   ├── rizom_connection.py # RizomUVConnection — Path A/B 자동 선택 (각 워커 안에서 사용)
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
