"""RizomUV 워커 서브프로세스 — 풀의 인스턴스 1개당 1프로세스.

왜 프로세스인가
==============
RizomUVLink 는 Boost.Python(.pyd) 확장이라 호출이 진행되는 동안 GIL 을 놓지 않는다.
→ 한 파이썬 프로세스 안에서 스레드를 N개 띄워도 RizomUVLink 호출은 GIL 에서 직렬화된다
  (실측: 24코어 머신에서도 2개 동시 speedup 0.9x). 그래서 인스턴스마다 *별도 프로세스*
  (각자 자기 GIL/cwd/stdout)로 띄워야 진짜 병렬 throughput 이 나온다. 프로세스가 분리되면
  os.chdir/stdout 같은 전역 부작용도 프로세스별로 자연 격리되어 런치 직렬화도 불필요해진다.

부모(MCP 서버)와의 통신
=====================
로컬 TCP 소켓으로 길이프리픽스(4바이트 big-endian) + UTF-8 JSON 을 주고받는다.
  요청: {"method": "<RizomUVConnection 메서드명>", "kwargs": {...}}  또는  {"cmd": "__shutdown__"}
  응답: 그 메서드의 반환 dict (메서드가 dict 가 아니면 {"ok": true, "result": ...} 로 감쌈),
        실패 시 {"ok": false, "error": "..."}.

stdout 격리: RizomUV 는 Popen(stdout 상속)으로 떠서 워커 stdout 에 쓸 수 있다. 워커는 IPC 를
소켓으로 하므로, 시작하자마자 워커의 fd1(stdout)을 devnull 로 영구 리다이렉트한다 → RizomUV
출력이 어디로 새도 IPC/부모와 무관.
"""

import os
import sys
import json
import socket
import struct


def _recv_n(sock: socket.socket, n: int):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _recv(sock: socket.socket):
    hdr = _recv_n(sock, 4)
    if hdr is None:
        return None
    (length,) = struct.unpack(">I", hdr)
    body = _recv_n(sock, length)
    if body is None:
        return None
    return json.loads(body.decode("utf-8"))


def _send(sock: socket.socket, obj) -> None:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sock.sendall(struct.pack(">I", len(data)) + data)


def _kill_child_processes() -> None:
    """이 워커가 부모인 자식 프로세스(= RizomUVLink 가 띄운 rizomuv.exe)를 강제 종료한다.

    부모(MCP 서버)가 죽어 워커가 소켓 EOF 로 빠져나오는 경우, 워커 자신이 자식 rizomuv 를
    정리해 orphan 을 막는다. taskkill 은 PPID 필터를 지원하지 않으므로 ctypes 로 프로세스
    스냅샷을 떠서 PPID == 내 pid 인 것들을 TerminateProcess 한다 (Windows, 외부 의존성 없음).
    """
    try:
        import ctypes
        from ctypes import wintypes

        me = os.getpid()
        kernel32 = ctypes.windll.kernel32

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260),
            ]

        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
        if snapshot == -1 or snapshot == 0xFFFFFFFF:
            return
        try:
            entry = PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
            ok = kernel32.Process32First(snapshot, ctypes.byref(entry))
            while ok:
                if entry.th32ParentProcessID == me:
                    h = kernel32.OpenProcess(0x0001, False, entry.th32ProcessID)  # PROCESS_TERMINATE
                    if h:
                        kernel32.TerminateProcess(h, 1)
                        kernel32.CloseHandle(h)
                ok = kernel32.Process32Next(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
    except Exception:
        pass


def main() -> None:
    port = int(sys.argv[1])
    # src 디렉터리를 import 경로에 (config / rizom_connection).
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    # 부모의 리스닝 소켓에 연결.
    sock = socket.create_connection(("127.0.0.1", port), timeout=30)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    # RizomUV 출력이 IPC 를 오염시키지 못하게 워커 stdout 을 devnull 로 영구 격리.
    try:
        sys.stdout.flush()
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.close(devnull)
    except Exception:
        pass

    import config  # noqa: F401  (env 기반 설정 1회 계산)
    from rizom_connection import RizomUVConnection

    rc = RizomUVConnection()

    try:
        while True:
            req = _recv(sock)
            if req is None:
                break
            if req.get("cmd") == "__shutdown__":
                break
            method = req.get("method")
            kwargs = req.get("kwargs") or {}
            try:
                fn = getattr(rc, method, None)
                if not callable(fn):
                    _send(sock, {"ok": False, "error": f"unknown method: {method}"})
                    continue
                result = fn(**kwargs)
                if not isinstance(result, dict):
                    result = {"ok": True, "result": result}
                _send(sock, result)
            except Exception as e:  # noqa: BLE001 — 워커는 절대 죽지 않고 에러를 응답으로
                _send(sock, {"ok": False, "error": f"{type(e).__name__}: {e}"})
    finally:
        try:
            rc.quit()        # 이 워커의 RizomUV 라이브 세션 종료(graceful)
        except Exception:
            pass
        # RizomUVLink 가 띄운 자식 rizomuv.exe 가 orphan 으로 남지 않게 강제 정리
        # (부모 사망→EOF 로 여기 도달하는 경로 대비. link.Quit 이 완전 종료를 보장 못 함).
        _kill_child_processes()
        try:
            sock.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
