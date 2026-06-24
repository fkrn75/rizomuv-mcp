"""
RizomUV 연결/실행 모듈  (SP-MCP 의 PainterRemote 에 대응).

아키텍처 (Scout 리서치 + 팀리드 확정, DUAL PATH):

  Path A — RizomUVLink (v2022.2+, PRIMARY):
      RizomUV 설치 폴더에 번들된 `rizomuvlink` 파이썬 모듈(ZeroMQ/TCP)을 사용.
          link = CRizomUVLink()
          port = link.RunRizomUV()              # exe 실행, 동적 TCP 포트 반환
          link.Load({"File": {"Path": ...}})
          link.Cut({...}); link.Unfold({}); link.Pack({"Translate": True})
          link.Save({"File": {"Path": ...}})
          link.Quit({})

  Path B — CLI + Lua 스크립트 (v<2022.2 / headless, FALLBACK):
          subprocess.run([rizomuv_exe, "-cfi", lua_script_path])
      -cfi = command file input. RizomUV 가 .lua 를 읽어 실행 후 종료.
      Lua API: ZomLoad / ZomCut / ZomSelect / ZomUnfold / ZomPack / ZomSave / ZomQuit

핵심 원칙: `rizomuvlink` 는 우리 venv 가 아니라 RizomUV 설치 폴더에 있으므로
           **항상 런타임에 lazy import** 한다. RizomUV 미설치 환경에서도
           이 모듈의 import / check_connection 은 절대 깨지지 않는다.
           RizomUVLink 가 없으면 도구는 친절한 에러를 반환한다 (크래시 X).
"""

import os
import sys
import json
import shutil
import tempfile
import threading
import subprocess
from pathlib import Path
from typing import Any, Optional

import config


# ⚠️ 프로세스 전역 launch 락 (모든 RizomUVConnection 인스턴스가 공유).
# RunRizomUV 가 일으키는 os.chdir + os.dup2(stdout fd 1) 는 *프로세스 전역* 부작용이라
# per-connection 락으로는 보호되지 않는다 — 서로 다른 connection(다른 per-instance 락)이
# 동시에 launch 하면 같은 cwd/fd1 을 망친다. 따라서 launch 직렬화는 반드시 모듈 전역 락으로.
# (풀의 _launch_lock 은 풀 경유 호출만 막지만, _probe 등 풀 밖 connection 도 같은 프로세스에서
#  _ensure_link 를 부를 수 있으므로 권위 있는 직렬화는 여기, 부작용의 발생 지점에 둔다.)
_LAUNCH_GLOBAL_LOCK = threading.Lock()


class RizomUVError(Exception):
    """RizomUV 연결/실행 실패."""
    pass


class RizomUVConnection:
    """RizomUV 자동화 연결 클래스 (Path A 우선, Path B 폴백)."""

    def __init__(
        self,
        exe: Optional[str] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        timeout: Optional[int] = None,
    ):
        self.exe = exe or config.RIZOMUV_EXE
        self.host = host or config.RIZOMUV_HOST
        self.port = port if port is not None else config.RIZOMUV_PORT
        self.timeout = timeout or config.RIZOMUV_TIMEOUT
        self._link = None  # 활성 RizomUVLink 인스턴스 (Path A)

    # ------------------------------------------------------------------
    # 탐지 / 상태
    # ------------------------------------------------------------------
    def exe_path(self) -> Optional[str]:
        """사용 가능한 RizomUV 실행 파일 경로 (없으면 None)."""
        if Path(self.exe).is_file():
            return self.exe
        return shutil.which("rizomuv") or shutil.which("rizomuv.exe")

    def _try_import_link(self):
        """번들된 rizomuvlink 모듈을 lazy import 한다.

        실패하면 (RizomUV 미설치 / 구버전 / pyzmq 없음) ImportError 를 던진다.
        절대 import-time 에 호출하지 말 것 — 반드시 런타임에서.
        """
        for link_dir in config.rizomuvlink_search_dirs():
            if link_dir and link_dir not in sys.path:
                sys.path.insert(0, link_dir)
        # 패키지/모듈명이 버전별로 약간 다를 수 있어 두 형태 모두 시도
        try:
            from rizomuvlink import CRizomUVLink  # type: ignore
            return CRizomUVLink
        except ImportError:
            from RizomUVLink import CRizomUVLink  # type: ignore
            return CRizomUVLink

    def check_connection(self) -> dict:
        """RizomUV 가용성 점검. 절대 예외를 던지지 않고 dict 로 보고한다.

        - exe 존재 여부
        - Path A(RizomUVLink) import 가능 여부
        - Path B(CLI) 사용 가능 여부 (exe 만 있으면 가능)
        - pyzmq(우리 venv 의존성) 존재 여부
        """
        exe = self.exe_path()
        result = {
            "ok": False,
            "exe": exe or self.exe,
            "exe_found": exe is not None,
            "host": self.host,
            "port": self.port,
            "path_a_rizomuvlink": False,
            "path_b_cli_lua": exe is not None,
            "pyzmq": False,
            "detail": "",
        }

        try:
            import zmq  # noqa: F401
            result["pyzmq"] = True
        except ImportError:
            result["pyzmq"] = False

        if exe is None:
            result["detail"] = (
                "RizomUV not found. Set RIZOMUV_HOME or RIZOMUV_EXE, "
                "or install RizomUV 2022.2+."
            )
            return result

        try:
            self._try_import_link()
            result["path_a_rizomuvlink"] = True
        except Exception:
            result["path_a_rizomuvlink"] = False

        result["ok"] = True
        if result["path_a_rizomuvlink"]:
            result["detail"] = "RizomUV found. Path A (RizomUVLink live socket) available."
        else:
            reason = (
                "RizomUVLink requires RizomUV 2022.2+ (this build predates the bundled link)"
                if result["pyzmq"]
                else "pyzmq missing in this venv"
            )
            result["detail"] = (
                f"RizomUV exe found, but Path A (RizomUVLink live socket) unavailable — {reason}. "
                "Using Path B (CLI + Lua batch)."
            )
        return result

    def _require_exe(self) -> str:
        exe = self.exe_path()
        if exe is None:
            raise RizomUVError(
                "RizomUVLink not available — set RIZOMUV_HOME or install RizomUV 2022.2+."
            )
        return exe

    # ------------------------------------------------------------------
    # Path A — RizomUVLink (live ZMQ)
    # ------------------------------------------------------------------
    def _ensure_link(self):
        """RizomUVLink 인스턴스를 생성하고 RizomUV 를 실행한다 (필요 시).

        ⚠️ 서브에이전트/MCP 안전장치 — RunRizomUV 는 두 가지 전역 부작용을 일으킨다:
          (1) RizomUV 를 subprocess.Popen(stdout 상속)으로 띄운다 → RizomUV 가 stdout 에
              쓰면 MCP stdio(JSON-RPC)가 오염돼 연결이 끊긴다.
          (2) os.chdir(설치폴더) 로 프로세스 작업 디렉터리를 바꾼다.
        따라서 실행 구간 동안 fd1(stdout)을 devnull 로 돌리고, 끝나면 cwd 와 stdout 을 원복한다.
        (보호에 실패해도 예외 없이 degrade — 기능은 계속 동작.)

        ⚠️ 동시성 — 위 (1)(2)+dup2 는 *프로세스 전역* 상태다. 두 connection 이 동시에
        launch 하면 서로의 cwd/fd1 을 덮어쓴다(check-then-act 레이스). 따라서 launch
        구간 전체를 모듈 전역 `_LAUNCH_GLOBAL_LOCK` 으로 직렬화한다. 더블체크 패턴으로
        fast-path(이미 launch 됨)는 락 없이 통과 → 이미 떠 있는 인스턴스의 병렬 작업은 무경합.
        """
        if self._link is not None:                  # fast path — 락 없음(병렬 작업 보존)
            return self._link
        with _LAUNCH_GLOBAL_LOCK:                    # launch 할 때만 경합 → 프로세스 전역 직렬화
            if self._link is not None:              # 락 안에서 재확인(다른 스레드가 먼저 launch)
                return self._link
            self._require_exe()
            CRizomUVLink = self._try_import_link()
            link = CRizomUVLink()

            saved_cwd = os.getcwd()
            saved_stdout_fd = None
            devnull_fd = None
            try:
                try:
                    sys.stdout.flush()
                    saved_stdout_fd = os.dup(1)                 # 진짜 stdout 보관
                    devnull_fd = os.open(os.devnull, os.O_WRONLY)
                    os.dup2(devnull_fd, 1)                       # fd1 → devnull (Popen 이 상속)
                except Exception:
                    saved_stdout_fd = None                       # 보호 실패 → 그냥 진행
                try:
                    assigned = link.RunRizomUV()
                except TypeError:
                    # 일부 버전은 exe 경로를 인자로 받는다
                    assigned = link.RunRizomUV(self.exe_path())
            finally:
                if saved_stdout_fd is not None:                  # stdout 원복
                    try:
                        os.dup2(saved_stdout_fd, 1)
                        os.close(saved_stdout_fd)
                    except Exception:
                        pass
                if devnull_fd is not None:
                    try:
                        os.close(devnull_fd)
                    except Exception:
                        pass
                try:
                    os.chdir(saved_cwd)                          # cwd 원복
                except Exception:
                    pass

            if assigned:
                self.port = assigned
            self._link = link
            return link

    def _path_a_available(self) -> bool:
        try:
            self._try_import_link()
            return self.exe_path() is not None
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Path B — CLI + Lua 스크립트 파일
    # ------------------------------------------------------------------
    def execute_lua(self, script: str) -> dict:
        """임의 Lua 스크립트를 임시 파일로 쓰고 `rizomuv.exe -cfi` 로 실행 (Path B)."""
        exe = self._require_exe()
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".lua", delete=False, encoding="utf-8"
        )
        try:
            tmp.write(script)
            tmp.close()
            proc = subprocess.run(
                [exe, "-cfi", tmp.name],
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            return {
                "ok": proc.returncode == 0,
                "path": "B",
                "returncode": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # 개별 작업 — Path A 가능하면 라이브 명령, 아니면 Lua 스니펫 누적
    # ------------------------------------------------------------------
    @staticmethod
    def _lua_path(p: str) -> str:
        """윈도우 경로를 Lua 문자열 리터럴용으로 이스케이프."""
        return p.replace("\\", "\\\\")

    def load_mesh(self, input_path: str, import_groups: bool = True) -> dict:
        """메시 파일(.fbx/.obj)을 RizomUV 로 로드한다."""
        if self._path_a_available():
            link = self._ensure_link()
            link.Load({"File": {"Path": input_path, "ImportGroups": import_groups, "XYZUVW": True}})
            return {"ok": True, "path": "A", "op": "load", "input": input_path}
        script = (
            f'ZomLoad({{File={{Path="{self._lua_path(input_path)}", '
            f"ImportGroups={'true' if import_groups else 'false'}, XYZUVW=true}}}})\nZomQuit()\n"
        )
        return self.execute_lua(script)

    def cut_by_sharp_edges(self, angle: float = 45.0) -> dict:
        """샤프 에지(법선 각도 임계값) 기준으로 심(seam)을 선택해 자른다.

        RizomUV 공식 API 문서(설치폴더 doc/index.html) 검증: Select 의
        `Auto.SharpEdges.AngleMin` 으로 '연결 폴리곤 법선 각도 > AngleMin' 에지를
        자동 선택한 뒤 Cut(PrimType=Edge) 한다.
        (구 PolyEdgeSel 은 'not recognized' 에러, Auto.FlatAngle 은 무시되는 no-op 였음 — 둘 다 교체됨.)
        """
        if self._path_a_available():
            link = self._ensure_link()
            link.Select({"PrimType": "Edge", "Select": True, "ResetBefore": True,
                         "WorkingSet": "Visible", "Auto": {"SharpEdges": {"AngleMin": angle}}})
            link.Cut({"PrimType": "Edge"})
            return {"ok": True, "path": "A", "op": "cut", "angle": angle}
        sel = ('ZomSelect({PrimType="Edge", Select=true, ResetBefore=true, '
               'WorkingSet="Visible", Auto={SharpEdges={AngleMin=' + str(angle) + '}}})')
        script = sel + '\nZomCut({PrimType="Edge"})\nZomQuit()\n'
        return self.execute_lua(script)

    def select_primitives(self, mode: str = "Island", select_all: bool = True) -> dict:
        """프리미티브(아일랜드/폴리곤 등) 선택."""
        if self._path_a_available():
            link = self._ensure_link()
            link.Select({"PrimType": mode, "Select": select_all, "WorkingSet": "Visible"})
            return {"ok": True, "path": "A", "op": "select", "mode": mode}
        sel = "true" if select_all else "false"
        script = f'ZomSelect({{PrimType="{mode}", Select={sel}, WorkingSet="Visible"}})\nZomQuit()\n'
        return self.execute_lua(script)

    def unfold_uvs(self) -> dict:
        """선택된 UV 를 언폴드한다."""
        if self._path_a_available():
            link = self._ensure_link()
            link.Unfold({})
            return {"ok": True, "path": "A", "op": "unfold"}
        return self.execute_lua("ZomUnfold({})\nZomQuit()\n")

    def pack_uvs(self, translate: bool = True, map_resolution: Optional[int] = None,
                 texel_density: Optional[float] = None, rotate: bool = True) -> dict:
        """UV 아일랜드를 패킹한다.

        map_resolution: 최종 텍스처 맵 해상도(px). texel_density: 모든 아일랜드 목표 텍셀 밀도.
        rotate: 패킹 시 아일랜드 회전 허용(끄면 Rotate.Mode=0).
        (파라미터 출처: 설치폴더 doc/index.html — Pack.MapResolution / Pack.Scaling.TexelDensity / Pack.Rotate.Mode)
        """
        params = {"Translate": translate}
        if map_resolution:
            params["MapResolution"] = int(map_resolution)
        if texel_density:
            params["Scaling"] = {"TexelDensity": float(texel_density)}
        if not rotate:
            params["Rotate"] = {"Mode": 0}
        if self._path_a_available():
            self._ensure_link().Pack(params)
            return {"ok": True, "path": "A", "op": "pack", "params": params}
        return self.execute_lua(f"ZomPack({self._lua_value(params)})\nZomQuit()\n")

    def save_mesh(self, output_path: str) -> dict:
        """현재 메시를 파일(.fbx/.obj)로 저장한다."""
        if self._path_a_available():
            link = self._ensure_link()
            link.Save({"File": {"Path": output_path}})
            return {"ok": True, "path": "A", "op": "save", "output": output_path}
        script = f'ZomSave({{File={{Path="{self._lua_path(output_path)}"}}}})\nZomQuit()\n'
        return self.execute_lua(script)

    # ------------------------------------------------------------------
    # 편의 파이프라인: Load → Cut → Unfold → Pack → Save
    # ------------------------------------------------------------------
    def unwrap_file(
        self,
        input_path: str,
        output_path: str,
        cut_angle: float = 45.0,
        import_groups: bool = True,
        pack: bool = True,
    ) -> dict:
        """전체 UV 언랩 파이프라인을 한 번에 실행한다.

        Path A 가능 시: 하나의 라이브 세션에서 순차 실행.
        아니면 Path B: 단일 Lua 스크립트로 묶어 실행.
        """
        self._require_exe()

        if self._path_a_available():
            link = self._ensure_link()
            try:
                link.Load({"File": {"Path": input_path, "ImportGroups": import_groups, "XYZUVW": True}})
                link.Select({"PrimType": "Edge", "Select": True, "ResetBefore": True,
                             "WorkingSet": "Visible", "Auto": {"SharpEdges": {"AngleMin": cut_angle}}})
                link.Cut({"PrimType": "Edge"})
                link.Unfold({})
                if pack:
                    link.Pack({"Translate": True})
                link.Save({"File": {"Path": output_path}})
                return {"ok": True, "path": "A", "input": input_path, "output": output_path}
            except Exception as e:
                # 라이브 세션 실패 → Path B 폴백
                result = self._unwrap_pathB(input_path, output_path, cut_angle, import_groups, pack)
                result["path_a_error"] = str(e)
                return result

        return self._unwrap_pathB(input_path, output_path, cut_angle, import_groups, pack)

    def _unwrap_pathB(self, input_path, output_path, cut_angle, import_groups, pack) -> dict:
        inp = self._lua_path(input_path)
        outp = self._lua_path(output_path)
        ig = "true" if import_groups else "false"
        pack_line = "ZomPack({Translate=true})\n" if pack else ""
        sel = ('ZomSelect({PrimType="Edge", Select=true, ResetBefore=true, '
               'WorkingSet="Visible", Auto={SharpEdges={AngleMin=' + str(cut_angle) + '}}})')
        script = (
            f'ZomLoad({{File={{Path="{inp}", ImportGroups={ig}, XYZUVW=true}}}})\n'
            + sel + '\n'
            f'ZomCut({{PrimType="Edge"}})\n'
            f"ZomUnfold({{}})\n"
            f"{pack_line}"
            f'ZomSave({{File={{Path="{outp}"}}}})\n'
            f"ZomQuit()\n"
        )
        result = self.execute_lua(script)
        result["input"] = input_path
        result["output"] = output_path
        return result

    # ------------------------------------------------------------------
    # 추가 작업 (Optimize / Weld / RasterExport / get_info / 범용 실행)
    # 파라미터는 RizomUV 설치폴더 doc/index.html(공식 API 레퍼런스) 기준으로 검증됨.
    # ------------------------------------------------------------------
    @staticmethod
    def _lua_value(v) -> str:
        """파이썬 값을 Lua 리터럴로 직렬화 (Path B 스크립트 생성용 — 중첩 테이블 안전)."""
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        if isinstance(v, str):
            return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
        if isinstance(v, dict):
            return "{" + ", ".join(f"{k}={RizomUVConnection._lua_value(val)}" for k, val in v.items()) + "}"
        if isinstance(v, (list, tuple)):
            return "{" + ", ".join(RizomUVConnection._lua_value(x) for x in v) + "}"
        return "nil"

    def optimize_uvs(self) -> dict:
        """언폴드 결과를 추가 최적화한다(스트레칭/왜곡 감소)."""
        if self._path_a_available():
            self._ensure_link().Optimize({})
            return {"ok": True, "path": "A", "op": "optimize"}
        return self.execute_lua("ZomOptimize({})\nZomQuit()\n")

    def weld_uvs(self) -> dict:
        """UV 공간에서 겹치는(superposed) 에지를 다시 붙인다(심 병합)."""
        params = {"PrimType": "Edge", "WorkingSet": "Visible"}
        if self._path_a_available():
            self._ensure_link().Weld(params)
            return {"ok": True, "path": "A", "op": "weld"}
        return self.execute_lua(f"ZomWeld({self._lua_value(params)})\nZomQuit()\n")

    def export_uv_layout(self, output_path: str, width: int = 1024, height: int = 1024) -> dict:
        """현재 UV 레이아웃을 이미지 파일(PNG/TIFF/…)로 래스터 익스포트한다."""
        params = {"FilePath": output_path, "Width": int(width), "Height": int(height)}
        if self._path_a_available():
            self._ensure_link().RasterExport(params)
            return {"ok": True, "path": "A", "op": "raster_export", "output": output_path}
        return self.execute_lua(f"ZomRasterExport({self._lua_value(params)})\nZomQuit()\n")

    def get_info(self) -> dict:
        """연결된 RizomUV 버전 + 설정 스냅샷(라이브 세션 필요)."""
        if self._path_a_available():
            link = self._ensure_link()
            return {"ok": True, "path": "A", "rizomuv_version": link.RizomUVVersion(),
                    "config": config.as_dict()}
        return {"ok": False, "error": "get_info needs the live socket (Path A / RizomUV 2022.2+)."}

    def execute_command(self, command: str, parameters: Optional[dict] = None) -> dict:
        """라이브 세션에서 임의의 RizomUV 명령을 이름+파라미터로 직접 실행한다(link.Execute).

        RizomUV 전체 태스크 API(Load/Select/Cut/Unfold/Pack/Optimize/Weld/RasterExport/
        IslandGroups/SymmetrySet/Move/Deform/Set/Get …)에 도달하는 범용 라이브 이스케이프 해치.
        파라미터 스키마는 RizomUV 설치폴더 doc/index.html 참조.
        """
        if not self._path_a_available():
            raise RizomUVError(
                "execute_command requires the live socket (Path A / RizomUV 2022.2+). "
                "For batch execution use execute_lua instead."
            )
        link = self._ensure_link()
        result = link.Execute(command, parameters or {})
        return {"ok": True, "path": "A", "command": command, "result": result}

    def close_session(self) -> dict:
        """현재 RizomUV 라이브 세션을 닫는다(창 종료). 다음 작업 시 자동으로 다시 열린다."""
        if self._link is not None:
            self.quit()
            return {"ok": True, "op": "close_session", "detail": "RizomUV live session closed."}
        return {"ok": True, "op": "close_session", "detail": "No live session was open."}

    def quit(self) -> None:
        """활성 RizomUVLink 세션 종료 (Path A)."""
        if self._link is not None:
            try:
                self._link.Quit({})
            except Exception:
                pass
            self._link = None


if __name__ == "__main__":
    conn = RizomUVConnection()
    print(json.dumps(conn.check_connection(), ensure_ascii=False, indent=2))
