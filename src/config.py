"""
RizomUV MCP - Configuration

모든 설정은 환경 변수로 오버라이드 가능. 윈도우 기본값 제공.

환경 변수:
    RIZOMUV_HOME    RizomUV 설치 디렉터리 (rizomuvlink 모듈/rizomuv.exe 가 있는 폴더)
    RIZOMUV_EXE     rizomuv.exe 절대 경로 (RIZOMUV_HOME 보다 우선)
    RIZOMUV_HOST    라이브 소켓 연결용 호스트 (기본 127.0.0.1)
    RIZOMUV_PORT    라이브 소켓 연결용 포트  (기본 0 = RizomUVLink 가 자동 할당)
    RIZOMUV_TIMEOUT 연결/실행 타임아웃 초 (기본 600)
    RIZOMUV_POOL_SIZE 인스턴스 풀 최대 크기 — 동시에 띄울 수 있는 RizomUV exe 개수
                    (기본 2). 각 인스턴스는 독립 RizomUVLink/ZMQ 포트를 가진다.
                    1 이면 사실상 단일 세션(기존 동작)과 동일하다.

설치 탐지 우선순위 (RIZOMUV_EXE 미지정 시):
    1) RIZOMUV_HOME\\rizomuv.exe
    2) 윈도우 레지스트리  HKLM\\SOFTWARE\\Rizom Lab  (버전 서브키 최신→구버전)
    3) 흔한 Program Files 설치 경로 후보
    4) 가장 그럴듯한 기본값 (존재하지 않을 수 있음)

주의: 이 모듈은 RizomUV 가 설치되어 있지 않아도 import 가능해야 한다.
      파일 존재/레지스트리 조회 실패는 import 를 깨뜨리지 않는다.
"""

import os
import re
from pathlib import Path
from typing import Optional

# RizomUV 의 흔한 윈도우 설치 경로 후보들 (버전별로 폴더명이 다름).
_DEFAULT_EXE_CANDIDATES = [
    r"C:\Program Files\Rizom Lab\RizomUV 2023.0\rizomuv.exe",
    r"C:\Program Files\Rizom Lab\RizomUV 2022.2\rizomuv.exe",
    r"C:\Program Files\Rizom Lab\RizomUV VS RS 2022.2\rizomuv.exe",
    r"C:\Program Files\Rizom Lab\RizomUV VS RS 2022.1\rizomuv.exe",
    r"C:\Program Files\Rizom Lab\RizomUV VS 2020.1\rizomuv.exe",
]


def _home_exe() -> Optional[str]:
    """RIZOMUV_HOME 환경 변수가 가리키는 디렉터리에서 rizomuv.exe 를 찾는다."""
    home = os.environ.get("RIZOMUV_HOME")
    if not home:
        return None
    exe = Path(home) / "rizomuv.exe"
    return str(exe) if exe.is_file() else None


def _registry_exe() -> Optional[str]:
    """윈도우 레지스트리에서 RizomUV 설치 경로를 조회한다 (최신 버전 우선).

    RizomUVLink 자체가 사용하는 방식. 키/값 레이아웃은 버전별로 다를 수 있어
    여러 후보 키를 관대하게 탐색한다. 실패 시 None (절대 예외 전파 안 함).
    """
    try:
        import winreg  # 윈도우 전용; 다른 OS 에서는 import 실패 → None
    except ImportError:
        return None

    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Rizom Lab"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Rizom Lab"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Rizom Lab"),
    ]
    for root, base in roots:
        try:
            with winreg.OpenKey(root, base) as key:
                subnames = []
                i = 0
                while True:
                    try:
                        subnames.append(winreg.EnumKey(key, i))
                    except OSError:
                        break
                    i += 1
                # 최신 버전 우선 (예: "RizomUV 2029.x" → ... → "RizomUV 2022.2")
                for subname in sorted(subnames, reverse=True):
                    try:
                        with winreg.OpenKey(key, subname) as subkey:
                            for value_name in ("Path", "InstallDir", "InstallPath", ""):
                                try:
                                    val, _ = winreg.QueryValueEx(subkey, value_name)
                                except OSError:
                                    continue
                                if not val:
                                    continue
                                p = Path(val)
                                exe = p if p.suffix.lower() == ".exe" else p / "rizomuv.exe"
                                if exe.is_file():
                                    return str(exe)
                    except OSError:
                        continue
        except OSError:
            continue
    return None


def _version_key(folder_name: str) -> tuple:
    """설치 폴더명에서 (연도, 마이너) 버전을 뽑는다. 예: 'RizomUV VS RS 2022.1' → (2022, 1)."""
    m = re.search(r"(\d{4})\.(\d+)", folder_name)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _scan_program_files() -> Optional[str]:
    """`...\\Rizom Lab\\<버전>\\rizomuv.exe` 를 스캔해 최신 버전의 exe 를 고른다.

    하드코딩 후보 목록보다 견고하다 — 설치된 어떤 버전(2018/2022.1/2023…)이든
    폴더명/접미사(VS·RS·VS RS) 변형과 무관하게 자동 인식하고, 버전 번호 기준
    최신을 선택한다. 실패 시 None (예외 전파 안 함).
    """
    bases = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ]
    found = []
    for base in bases:
        lab = Path(base) / "Rizom Lab"
        if not lab.is_dir():
            continue
        try:
            for sub in lab.iterdir():
                exe = sub / "rizomuv.exe"
                if exe.is_file():
                    found.append((_version_key(sub.name), str(exe)))
        except OSError:
            continue
    if not found:
        return None
    found.sort(key=lambda t: t[0], reverse=True)  # 최신 버전 우선
    return found[0][1]


def _resolve_exe() -> str:
    """RizomUV 실행 파일 경로를 결정한다.

    RIZOMUV_EXE → RIZOMUV_HOME → 레지스트리 → Program Files 스캔 → 경로 후보 → 기본값 순.
    """
    env = os.environ.get("RIZOMUV_EXE")
    if env:
        return env

    home = _home_exe()
    if home:
        return home

    reg = _registry_exe()
    if reg:
        return reg

    scan = _scan_program_files()
    if scan:
        return scan

    for candidate in _DEFAULT_EXE_CANDIDATES:
        if Path(candidate).is_file():
            return candidate

    # 아무것도 못 찾으면 가장 그럴듯한 기본값 (존재하지 않을 수 있음).
    return _DEFAULT_EXE_CANDIDATES[0]


# 설정 값 (import 시 1회 계산). 환경 변수로 전부 오버라이드 가능.
RIZOMUV_EXE: str = _resolve_exe()
RIZOMUV_HOST: str = os.environ.get("RIZOMUV_HOST", "127.0.0.1")
# 기본 포트 0 = RizomUVLink.RunRizomUV() 가 자유 포트를 자동 할당하도록 둠.
RIZOMUV_PORT: int = int(os.environ.get("RIZOMUV_PORT", "0"))
RIZOMUV_TIMEOUT: int = int(os.environ.get("RIZOMUV_TIMEOUT", "600"))

# 인스턴스 풀 — 동시에 띄울 수 있는 RizomUV.exe 개수. 각 인스턴스는 full RizomUV 프로세스라
# RAM/GPU 가 무겁다 → 작은 기본값(2). 항상 >=1 로 클램프(0/음수는 1로).
RIZOMUV_POOL_SIZE: int = max(1, int(os.environ.get("RIZOMUV_POOL_SIZE", "2")))
# acquire() 가 빈 인스턴스를 기다리는 최대 초. 풀이 가득 차면 이 시간 뒤 친절한 에러.
RIZOMUV_ACQUIRE_TIMEOUT: int = int(os.environ.get("RIZOMUV_ACQUIRE_TIMEOUT", "120"))


def exe_exists() -> bool:
    """현재 설정된 RizomUV 실행 파일이 실제로 존재하는지 (호출 시점 확인)."""
    return Path(RIZOMUV_EXE).is_file()


def rizomuvlink_dir() -> Optional[str]:
    """번들된 rizomuvlink 파이썬 모듈이 들어 있을 (최상위) 디렉터리.

    RIZOMUV_HOME 이 지정되어 있으면 그것을, 아니면 rizomuv.exe 의 부모 폴더를 쓴다.
    """
    home = os.environ.get("RIZOMUV_HOME")
    if home and Path(home).is_dir():
        return home
    if exe_exists():
        return str(Path(RIZOMUV_EXE).parent)
    return None


def rizomuvlink_search_dirs() -> list:
    """`rizomuvlink` 모듈을 import 하기 위해 sys.path 에 넣어볼 부모 디렉터리 후보들.

    RizomUV 버전마다 모듈 위치가 다르다(설치 루트 / 번들 Lib\\site-packages /
    scripts·API 등 서브폴더). 흔한 후보 + 얕은 트리 스캔으로 패키지의 부모를 모은다.
    실패해도 예외를 던지지 않는다(빈 목록 또는 부분 목록 반환).
    """
    base = rizomuvlink_dir()
    if not base:
        return []
    root = Path(base)
    dirs = [base]
    for c in (root / "Lib" / "site-packages", root / "scripts", root / "API", root / "python"):
        if c.is_dir() and str(c) not in dirs:
            dirs.append(str(c))
    # 2022.2+ 레이아웃: <install>\RizomUVLink\RizomUVLink.py (+ RizomUVLinkBase.py,
    # win\rizomuvlink_pythonXX.pyd). 마커 파일을 찾아 그 '부모 폴더'를 sys.path 후보로 넣는다.
    # 직속 하위(prefix "") + 1~2단계 하위, 대소문자/패키지 변형 모두 커버.
    markers = ("RizomUVLink.py", "rizomuvlink.py", "RizomUVLinkBase.py", "rizomuvlink/__init__.py")
    try:
        for prefix in ("", "*/", "*/*/"):
            for m in markers:
                for hit in root.glob(prefix + m):
                    parent = str(hit.parent)
                    if parent not in dirs:
                        dirs.append(parent)
    except OSError:
        pass
    return dirs


def as_dict() -> dict:
    """현재 설정 스냅샷 (health check / 디버그용)."""
    return {
        "exe": RIZOMUV_EXE,
        "exe_exists": exe_exists(),
        "home": os.environ.get("RIZOMUV_HOME"),
        "host": RIZOMUV_HOST,
        "port": RIZOMUV_PORT,
        "timeout": RIZOMUV_TIMEOUT,
        "pool_size": RIZOMUV_POOL_SIZE,
        "acquire_timeout": RIZOMUV_ACQUIRE_TIMEOUT,
        "rizomuvlink_dir": rizomuvlink_dir(),
    }
