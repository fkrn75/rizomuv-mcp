"""Smoke check. Runs ALL async steps on ONE event loop (matches real MCP runtime,
where everything runs under a single asyncio.run(_amain())). RizomUV may or may not
be installed; both paths must be graceful.
"""
import sys, os, asyncio, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import config
import rizom_pool          # noqa: F401
import rizom_connection    # noqa: F401
import server

print("=== imports OK ===")
print("python:", sys.version.split()[0])
print("pool_size:", config.RIZOMUV_POOL_SIZE, "acquire_timeout:", config.RIZOMUV_ACQUIRE_TIMEOUT)
print("config keys:", sorted(config.as_dict().keys()))

# 1) tools enumerate (sync-safe: list_tools just builds dicts)
async def _list():
    return await server.list_tools()

async def _call(name, args):
    res = await server.call_tool(name, args)
    return json.loads(res[0].text)


async def main():
    tools = await _list()
    print("\n=== tools enumerate:", len(tools), "===")
    session_tools = [t.name for t in tools
                     if "session" in (t.inputSchema or {}).get("properties", {})]
    for t in tools:
        props = (t.inputSchema or {}).get("properties", {})
        print(f"  {t.name:20s} session={'session' in props}")
    print("session-aware:", session_tools)

    # 2) pool lazy at construction
    st = server.pool.status()
    print("\n=== pool status (lazy) ===")
    print(json.dumps(st, ensure_ascii=False))
    assert st["instances_created"] == 0, "pool must be lazy"
    assert st["size"] == config.RIZOMUV_POOL_SIZE

    # 3) check_connection: works with 0 instances + reports pool
    payload = await _call("check_connection", {})
    print("\n=== check_connection ===")
    print("ok:", payload.get("ok"), "exe_found:", payload.get("exe_found"),
          "path_a:", payload.get("path_a_rizomuvlink"))
    rizomuv_present = bool(payload.get("path_a_rizomuvlink"))
    assert "pool" in payload and "config" in payload
    assert "pool_size" in payload["config"]

    # 4) stateful call with session: runs (if RizomUV present) or friendly error (absent).
    payload = await _call("unfold_uvs", {"session": "s1"})
    print("\n=== unfold_uvs(session=s1) ===")
    print(json.dumps(payload, ensure_ascii=False))
    assert isinstance(payload, dict)

    st = server.pool.status()
    print("pool after s1:", json.dumps(st, ensure_ascii=False))
    if rizomuv_present:
        # an instance got pinned to s1 and holds a permit
        assert "s1" in st["sessions"], "session s1 should be pinned"

    # 5) close_session(s1): releases the instance + permit
    payload = await _call("close_session", {"session": "s1"})
    print("\n=== close_session(s1) ===")
    print(json.dumps(payload, ensure_ascii=False))
    assert payload.get("ok") is True
    st = server.pool.status()
    assert "s1" not in st["sessions"], "s1 must be released after close_session"

    # 6) close_session for never-opened session = ok no-op (must NOT release a permit)
    payload = await _call("close_session", {"session": "never"})
    print("\n=== close_session(never) ===")
    print(json.dumps(payload, ensure_ascii=False))
    assert payload.get("ok") is True

    # 7) permit accounting on the SAME loop: after pin+close, capacity must be full again.
    p = server.pool
    got = []
    try:
        for _ in range(p.size):
            await asyncio.wait_for(p._sem.acquire(), timeout=3.0)
            got.append(1)
    finally:
        for _ in got:
            p._sem.release()
    print("\n=== permit accounting: reacquired", len(got), "of", p.size, "===")
    assert len(got) == p.size, "semaphore permit leaked!"

    # 8) ephemeral atomic path: unwrap_file with a bogus path. If RizomUV present it will
    #    error inside the op (bad file) but MUST release the permit (try/finally). If absent,
    #    friendly error. Either way capacity is restored afterwards.
    payload = await _call("unwrap_file", {"input_path": "C:/nope/does_not_exist.fbx",
                                          "output_path": "C:/nope/out.fbx"})
    print("\n=== unwrap_file(bogus) ===")
    print(json.dumps(payload, ensure_ascii=False)[:200])
    got = []
    try:
        for _ in range(p.size):
            await asyncio.wait_for(p._sem.acquire(), timeout=3.0)
            got.append(1)
    finally:
        for _ in got:
            p._sem.release()
    assert len(got) == p.size, "ephemeral unwrap_file leaked a permit!"
    print("ephemeral permit restored:", len(got), "of", p.size)

    # 9) FIX-2 / global-launch-lock exercise: two distinct sessions launched CONCURRENTLY.
    #    With pool_size>=2 and RizomUV present, both must get DISTINCT launched instances,
    #    distinct ports, and the second affinity-acquire must wait for launch (no _link=None race).
    if rizomuv_present and p.size >= 2:
        r1, r2 = await asyncio.gather(
            _call("unfold_uvs", {"session": "pa"}),
            _call("unfold_uvs", {"session": "pb"}),
        )
        print("\n=== concurrent sessions pa/pb ===")
        print("pa:", json.dumps(r1, ensure_ascii=False))
        print("pb:", json.dumps(r2, ensure_ascii=False))
        st = server.pool.status()
        print("pool:", json.dumps(st, ensure_ascii=False))
        assert {"pa", "pb"} <= set(st["sessions"]), "both sessions must be pinned"
        launched = [i for i in st["instances"] if i["launched"]]
        ports = {i["port"] for i in launched if i["session_id"] in ("pa", "pb")}
        assert len(ports) == 2, f"two sessions must use two distinct ports, got {ports}"
        # re-acquire same session must reuse the SAME instance (affinity), no new launch
        before = server.pool.status()["instances_created"]
        await _call("optimize_uvs", {"session": "pa"})
        after = server.pool.status()["instances_created"]
        assert after == before, "same session must not create a new instance"
        await _call("close_session", {"session": "pa"})
        await _call("close_session", {"session": "pb"})
        print("concurrent-session test OK: 2 distinct live instances, affinity reuse confirmed")

    # cleanup any launched instances
    server.pool.close_all()
    print("\n=== SMOKE PASS ===")


asyncio.run(main())
