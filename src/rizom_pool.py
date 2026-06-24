"""RizomUV 인스턴스 풀 — 진짜 멀티에이전트 병렬성 (멀티프로세스 워커).

왜 멀티프로세스인가
==================
RizomUVLink 는 Boost.Python(.pyd) 확장이라 호출 중 GIL 을 놓지 않는다. 한 파이썬
프로세스 안에서 스레드(asyncio.to_thread)로 N개 인스턴스를 돌려도 RizomUVLink 호출은
GIL 에서 직렬화된다(실측: 24코어에서도 2개 동시 speedup 0.9x). 그래서 인스턴스마다
*별도 워커 프로세스*(worker.py, 각자 자기 GIL)로 띄우고, 부모(MCP 서버)는 로컬 TCP
소켓 IPC 로 명령을 분배한다 → 서로 다른 워커의 작업은 진짜 병렬로 실행된다.

부수 효과로 동시성이 단순해진다: RunRizomUV 의 os.chdir/stdout dup2 같은 *프로세스 전역*
부작용이 워커 프로세스별로 격리되므로, 부모 쪽 전역 launch 락이 더 이상 필요 없다.

동시성 모델
==========
- 워커당 `op_lock` — 한 워커는 동시에 한 RPC 만(워커 안에서 직렬). 서로 다른 워커는 병렬.
- 작업(블로킹 IPC)은 `asyncio.to_thread` 로 이벤트 루프 밖에서. 실제 계산은 워커 프로세스
  (자기 GIL)에서 도므로 to_thread 가 GIL 을 잡고 있어도 워커끼리는 진짜 병렬.
- `Semaphore(size)` — 동시 사용 워커 수(=동시 RizomUV.exe 수) 용량 게이트.

락 획득 순서(데드락 방지): _sem → _pool_lock(짧은 북키핑만, await 안 들고 있음) → op_lock.

세션 모델
=========
- ephemeral: session 없이 acquire→작업→release. 매 호출 permit 1개 점유/반납. (unwrap_file)
- session affinity: `session` 문자열을 주면 같은 워커에 핀. 최초 핀 때만 permit 점유,
  같은 session 의 이후 호출은 재사용. permit 은 close_session 시 반납.
- 레거시(session 미지정) stateful 호출은 예약 세션 "__default__" 로 매핑(하위호환: 단일
  공유 워커에서 직렬). 서로 다른 session 또는 ephemeral 호출이 병렬성을 얻는다.
"""

import os
import sys
import json
import socket
import struct
import asyncio
import subprocess
from typing import Optional

import config
from rizom_connection import RizomUVError


DEFAULT_SESSION = "__default__"
_WORKER_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker.py")


def _taskkill_tree(pid: int) -> None:
    """프로세스 트리(워커 + 그 자식 rizomuv.exe)를 강제 종료한다 (Windows, best-effort).

    `taskkill /T` 가 자식까지 함께 죽이므로 워커가 graceful 하게 못 끝낸 경우의
    orphan rizomuv.exe 까지 한 번에 정리된다.
    """
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


class RizomUVWorker:
    """풀이 관리하는 워커 1개 — 별도 Python 프로세스(자기 GIL) + 로컬 소켓 IPC.

    프로세스 안에서 RizomUVConnection 1개(자기 RizomUVLink/ZMQ 포트)를 소유한다.
    부모는 이 객체를 통해 RPC 로만 워커를 부린다.
    """

    def __init__(self, instance_id: int):
        self.id = instance_id
        self.op_lock = asyncio.Lock()          # 워커당 동시 1 RPC
        self.in_use = False
        self.session_id: Optional[str] = None
        self.started = False                   # 워커 프로세스 spawn + 연결 완료?
        self.holds_permit = False
        self.ipc_port: Optional[int] = None    # 부모↔워커 IPC 포트(디버그용)
        self._proc: Optional[subprocess.Popen] = None
        self._sock: Optional[socket.socket] = None

    # ---- 동기 헬퍼(블로킹 — to_thread 에서 호출) ----------------------------
    def _start_sync(self) -> None:
        """워커 서브프로세스 spawn + IPC 연결 수립."""
        lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            lsock.bind(("127.0.0.1", 0))
            lsock.listen(1)
            lsock.settimeout(60)
            port = lsock.getsockname()[1]
            self.ipc_port = port
            # 같은 venv(py3.10) 의 파이썬으로 워커 기동. stdio 는 전부 버림.
            self._proc = subprocess.Popen(
                [sys.executable, _WORKER_PY, str(port)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            conn, _ = lsock.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.settimeout(None)
            self._sock = conn
        finally:
            lsock.close()

    def _rpc_sync(self, method: str, kwargs: dict) -> dict:
        """블로킹 RPC(워커에 메서드 1개 요청). 길이프리픽스 JSON."""
        if self._sock is None:
            raise RizomUVError("worker not started")
        payload = json.dumps({"method": method, "kwargs": kwargs or {}}, ensure_ascii=False).encode("utf-8")
        self._sock.sendall(struct.pack(">I", len(payload)) + payload)
        hdr = self._recv_n(4)
        if hdr is None:
            raise RizomUVError("worker connection closed before response")
        (length,) = struct.unpack(">I", hdr)
        body = self._recv_n(length)
        if body is None:
            raise RizomUVError("worker connection closed mid-response")
        return json.loads(body.decode("utf-8"))

    def _recv_n(self, n: int):
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    # ---- 비동기 API ------------------------------------------------------
    async def call(self, method: str, **kwargs):
        """이 워커에서 RizomUVConnection 메서드 1개를 실행한다.

        op_lock 으로 워커당 동시 1요청을 보장하고, 블로킹 IPC 는 to_thread 로 오프루프.
        실제 RizomUV 작업은 워커 프로세스(자기 GIL)에서 도므로 다른 워커와 진짜 병렬.
        """
        async with self.op_lock:
            return await asyncio.to_thread(self._rpc_sync, method, kwargs)

    def close_sync(self) -> None:
        """워커 종료(동기·best-effort). atexit/close_all/discard 용.

        워커가 살아있으면 `taskkill /T`(트리)로 워커 + 그 자식 rizomuv.exe 를 한 번에 정리한다.
        link.Quit 은 rizomuv.exe 를 확실히 죽이지 못하고 taskkill 은 PPID 필터를 지원하지 않으므로,
        '살아있는 워커 pid 의 트리킬'이 orphan 방지에 가장 확실하다. (워커가 이미 죽었으면 pid 재사용
        위험 때문에 트리킬을 생략 — 그 자식 rizomuv 는 best-effort 로 누락될 수 있다.)
        """
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            _taskkill_tree(proc.pid)
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        if proc is not None:
            try:
                proc.wait(timeout=3)
            except Exception:
                pass

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def status(self) -> dict:
        return {
            "id": self.id,
            "in_use": self.in_use,
            "started": self.started,
            "alive": self.alive(),
            "session_id": self.session_id,
            "ipc_port": self.ipc_port,
        }


class RizomUVPool:
    """N개의 독립 워커 프로세스를 관리하는 풀.

    lazy: __init__ 에서는 어떤 프로세스도 띄우지 않는다(미설치/디버그 안전).
    워커는 acquire 시점에 필요한 만큼만 spawn 된다.
    """

    def __init__(self, size: Optional[int] = None):
        self.size = max(1, size if size is not None else config.RIZOMUV_POOL_SIZE)
        self._workers: list[RizomUVWorker] = []
        self._sessions: dict[str, RizomUVWorker] = {}
        self._pool_lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(self.size)

    # ---- 내부(반드시 _pool_lock 을 잡은 상태에서) -------------------------
    def _pick_or_create_free_locked(self) -> RizomUVWorker:
        for w in self._workers:
            if not w.in_use and w.session_id is None:
                return w
        w = RizomUVWorker(len(self._workers))
        self._workers.append(w)
        return w

    # ---- 워커 기동(프로세스 격리 → 전역 락 불필요, 워커별 op_lock 으로만 직렬) ----
    async def _ensure_started(self, w: RizomUVWorker) -> None:
        if w.started:
            return
        async with w.op_lock:                 # 기동 직렬화 + 첫 작업이 기동을 기다리게
            if w.started:
                return
            await asyncio.to_thread(w._start_sync)
            w.started = True

    # ---- acquire / release ----------------------------------------------
    async def acquire(self, session_id: Optional[str] = None,
                      timeout: Optional[float] = None) -> RizomUVWorker:
        # 이미 핀된 세션은 재사용(permit 추가 점유 없음). 단 기동 완료까지 대기 후 반환.
        if session_id is not None:
            async with self._pool_lock:
                w = self._sessions.get(session_id)
            if w is not None:
                if w.started and not w.alive():
                    # 핀된 워커가 죽었다 → 퇴출하고 아래로 흘러 새 워커를 잡는다.
                    await self.discard(w, session_id)
                else:
                    await self._ensure_started(w)
                    return w

        # 용량 게이트.
        try:
            await asyncio.wait_for(
                self._sem.acquire(),
                timeout=timeout if timeout is not None else config.RIZOMUV_ACQUIRE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise RizomUVError(
                f"RizomUV pool exhausted (size={self.size}); "
                "no worker became free within timeout. "
                "Raise RIZOMUV_POOL_SIZE or close idle sessions."
            )

        # permit 확보 → 워커 선택/생성(짧은 북키핑). 같은 session 동시 acquire 재확인.
        reuse_existing = None
        try:
            async with self._pool_lock:
                if session_id is not None and session_id in self._sessions:
                    reuse_existing = self._sessions[session_id]
                else:
                    w = self._pick_or_create_free_locked()
                    w.in_use = True
                    w.holds_permit = True
                    if session_id is not None:
                        w.session_id = session_id
                        self._sessions[session_id] = w
        except BaseException:
            self._sem.release()
            raise

        if reuse_existing is not None:
            self._sem.release()                # 경쟁에서 졌다 — 잡은 permit 반납
            await self._ensure_started(reuse_existing)
            return reuse_existing

        # 기동은 _pool_lock 밖에서(느림). 실패 시 점유/세션 핀 되돌리고 permit 반납.
        try:
            await self._ensure_started(w)
        except BaseException:
            async with self._pool_lock:
                w.in_use = False
                if session_id is not None and self._sessions.get(session_id) is w:
                    del self._sessions[session_id]
                w.session_id = None
                released = w.holds_permit
                w.holds_permit = False
            if released:
                self._sem.release()
            try:
                w.close_sync()
            except Exception:
                pass
            raise

        return w

    async def release(self, w: RizomUVWorker, session_id: Optional[str] = None) -> None:
        # 세션 핀된 워커는 ephemeral release 로 풀지 않는다(close_session 만).
        if w.session_id is not None and session_id is None:
            return
        async with self._pool_lock:
            w.in_use = False
            released = w.holds_permit
            w.holds_permit = False
        if released:
            self._sem.release()

    async def discard(self, w: RizomUVWorker, session_id: Optional[str] = None) -> None:
        """죽었거나 못 쓰게 된 워커를 풀에서 퇴출하고 permit 을 반납한다.

        세션 핀을 모두 풀고 `_workers` 에서 제거(재사용 방지)한 뒤 프로세스를 정리한다.
        워커 사망 시 호출하면 permit 누수/죽은 워커 재사용을 막는다.
        """
        async with self._pool_lock:
            if session_id is not None and self._sessions.get(session_id) is w:
                del self._sessions[session_id]
            for sid in [s for s, ww in self._sessions.items() if ww is w]:
                del self._sessions[sid]
            w.session_id = None
            w.in_use = False
            released = w.holds_permit
            w.holds_permit = False
            if w in self._workers:
                try:
                    self._workers.remove(w)
                except ValueError:
                    pass
        if released:
            self._sem.release()
        try:
            w.close_sync()
        except Exception:
            pass

    # ---- 세션 종료 -------------------------------------------------------
    async def close_session(self, session_id: str = DEFAULT_SESSION) -> dict:
        async with self._pool_lock:
            w = self._sessions.get(session_id)
        if w is None:
            return {"ok": True, "op": "close_session", "session": session_id,
                    "detail": "No live session was open for this session id."}
        # 세션 종료 = 그 워커를 완전히 정리한다: 프로세스 종료 → 워커 finally 가 자식
        # rizomuv.exe 까지 kill. link.Quit 만으론 rizomuv.exe 가 안 죽고 워커를 재사용하면
        # 새 인스턴스가 떠 orphan 이 쌓이던 문제를 차단한다. 슬롯(permit)은 풀에 반환.
        await self.discard(w, session_id)
        return {"ok": True, "op": "close_session", "session": session_id,
                "detail": "RizomUV session closed and worker released."}

    # ---- 상태 / 정리 -----------------------------------------------------
    def status(self) -> dict:
        workers = list(self._workers)
        return {
            "mode": "multiprocess",
            "size": self.size,
            "workers_created": len(workers),
            "workers_started": sum(1 for w in workers if w.started),
            "workers_in_use": sum(1 for w in workers if w.in_use),
            "sessions": sorted(self._sessions.keys()),
            "workers": [w.status() for w in workers],
        }

    def close_all(self) -> None:
        """모든 워커 프로세스를 종료(atexit 용 — 동기, best-effort)."""
        for w in list(self._workers):
            try:
                w.close_sync()
            except Exception:
                pass
