"""Tests for MAPDL process-tree cleanup: exit, restart, forced cleanup of a running solve,
orphan cleanup at server startup, and the --cleanup CLI. Localhost only, own work dir.

Run:  .venv-mapdl/Scripts/python.exe tests/test_cleanup.py
"""
import asyncio, json, os, shutil, socket, subprocess, sys, time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]

os.environ.setdefault("AWP_ROOT261", r"D:\Program Files\ANSYS\ANSYS Inc\v261")
SERVER = str(ROOT / "mapdl_mcp.py")
PORT = 18803
WORK = ROOT / "work-test"
PIDFILE = WORK / ".mapdl_pids.json"
ENV = dict(os.environ, MAPDL_MCP_WORKDIR=str(WORK), MAPDL_IDLE_MINUTES="0")

PREP = """/PREP7
ET,1,BEAM188
MP,EX,1,70e9
MP,PRXY,1,0.3
SECTYPE,1,BEAM,CSOLID
SECDATA,0.005
K,1,0,0,0
K,2,1,0,0
L,1,2
LESIZE,ALL,,,200
LMESH,ALL
NSEL,S,LOC,X,1
CM,TIP,NODE
NSEL,S,LOC,X,0
CM,BASE,NODE
ALLSEL
FINISH
/SOLU
ANTYPE,STATIC
NLGEOM,ON
NSUBST,3000,6000,3000
OUTRES,ALL,ALL
CMSEL,S,BASE
D,ALL,ALL,0
CMSEL,S,TIP
F,ALL,FY,-100
ALLSEL"""


def start_server():
    p = subprocess.Popen([sys.executable, SERVER, "--host", "127.0.0.1", "--port", str(PORT)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=ENV)
    for _ in range(80):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", PORT)) == 0:
                return p
        time.sleep(0.5)
    raise RuntimeError("server did not start")


def tracked_pids():
    return json.loads(PIDFILE.read_text())["pids"] if PIDFILE.exists() else []


def alive(pids):
    return [p for p in pids if psutil.pid_exists(p)]


def locks():
    return [p.name for p in WORK.glob("*.lock")]


def show(label, text, n=400):
    text = str(text)
    print(f"\n### {label}\n{text[:n]}{'...' if len(text) > n else ''}")
    return text


async def with_client(fn):
    from fastmcp import Client
    async with Client(f"http://127.0.0.1:{PORT}/mcp") as c:
        return await fn(c)


async def t1_exit_kills_tree(c):
    show("start", (await c.call_tool("mapdl_start", {"nproc": 2})).data)
    pids = tracked_pids()
    print("tracked pids:", pids, "alive:", len(alive(pids)))
    assert pids and alive(pids), "no tracked MAPDL pids"
    st = show("status", (await c.call_tool("mapdl_status", {})).data)
    assert "MAPDL processes in work dir:" in st
    show("input", (await c.call_tool("mapdl_input", {"commands": PREP})).data, 200)
    rep = show("exit", (await c.call_tool("mapdl_exit", {})).data)
    time.sleep(2)
    print("after exit -> alive:", alive(pids), "locks:", locks(), "pidfile:", PIDFILE.exists())
    assert not alive(pids) and not locks() and not PIDFILE.exists(), "T1 failed"
    return pids


async def t2_restart(c):
    # fresh empty session + exit must NOT clobber the good autosave.db from T1
    show("start fresh", (await c.call_tool("mapdl_start", {"nproc": 2})).data, 200)
    show("exit of empty session", (await c.call_tool("mapdl_exit", {})).data, 300)
    show("start + resume", (await c.call_tool("mapdl_start", {"nproc": 2, "resume_autosave": True})).data, 200)
    n = (await c.call_tool("mapdl_get", {"entity": "NODE", "entnum": 0, "item1": "COUNT"})).data
    print("node count after resume:", n)
    assert "201" in n, "autosave.db was clobbered by the empty session"
    old = tracked_pids()
    rep = show("restart", (await c.call_tool("mapdl_start", {"nproc": 2, "restart": True, "resume_autosave": True})).data)
    new = tracked_pids()
    print("old:", old, "alive:", alive(old), "| new:", new, "alive:", len(alive(new)))
    assert not alive(old) and alive(new) and set(new).isdisjoint(old), "T2 failed: old tree survived or no new tree"
    n = (await c.call_tool("mapdl_get", {"entity": "NODE", "entnum": 0, "item1": "COUNT"})).data
    print("node count after restart+resume:", n)
    assert "201" in n


async def t3_forced_cleanup_of_running_solve(c):
    r = show("solve (wait 0)", (await c.call_tool("mapdl_solve", {"wait_seconds": 0})).data, 150)
    time.sleep(3)
    st = show("solve_status", (await c.call_tool("mapdl_solve_status", {})).data, 300)
    assert "state=running" in st, "solve finished too fast for this test; increase NSUBST"
    pids = tracked_pids()
    b = show("cleanup without force", (await c.call_tool("mapdl_cleanup", {})).data, 200)
    assert b.startswith("BUSY"), "cleanup should refuse while solve holds the lock"
    rep = show("cleanup force", (await c.call_tool("mapdl_cleanup", {"force": True})).data, 500)
    time.sleep(2)
    print("after forced cleanup -> alive:", alive(pids), "locks:", locks())
    assert not alive(pids) and not locks(), "T3 failed"
    st = show("status", (await c.call_tool("mapdl_status", {})).data, 300)
    assert "not running" in st and "processes in work dir: 0" in st
    ss = show("solve_status after cleanup", (await c.call_tool("mapdl_solve_status", {})).data, 200)
    assert "state=failed" in ss
    # next call relaunches + resumes autosave.db (saved after the PREP/SOLU block)
    n = show("get after cleanup (relaunch+resume)", (await c.call_tool("mapdl_get", {"entity": "NODE", "entnum": 0, "item1": "COUNT"})).data)
    assert "201" in n, "model not resumed"
    show("input works again", (await c.call_tool("mapdl_input", {"commands": "/PREP7\nFINISH"})).data, 120)
    return tracked_pids()


async def t4_leave_orphan(c):
    show("start (will be orphaned)", (await c.call_tool("mapdl_start", {"nproc": 2})).data, 200)
    return tracked_pids()


def main():
    if WORK.exists():
        r = subprocess.run([sys.executable, SERVER, "--cleanup"], capture_output=True, text=True, env=ENV, timeout=120)
        print("pre-test --cleanup:", r.stdout.strip()[:300])
        shutil.rmtree(WORK)
    WORK.mkdir()
    srv = start_server()
    try:
        asyncio.run(with_client(t1_exit_kills_tree))
        asyncio.run(with_client(t2_restart))
        asyncio.run(with_client(t3_forced_cleanup_of_running_solve))
        orphan = asyncio.run(with_client(t4_leave_orphan))
    finally:
        srv.kill()  # abrupt death, like taskkill /F on the server: MAPDL is now orphaned
        srv.wait(10)
    time.sleep(2)
    print("\n### orphan after server kill -> alive:", alive(orphan), "locks:", locks())
    assert alive(orphan), "test setup: orphan should still be alive"

    # T5: a new server must clean the orphan at startup and be able to launch MAPDL
    srv = start_server()
    try:
        time.sleep(1)
        print("### after new server startup -> orphan alive:", alive(orphan), "locks:", locks())
        assert not alive(orphan), "T5 failed: orphan survived server startup"

        async def t5(c):
            show("start after orphan cleanup", (await c.call_tool("mapdl_start", {"nproc": 2})).data, 200)
            return tracked_pids()
        orphan2 = asyncio.run(with_client(t5))
        assert alive(orphan2)
    finally:
        srv.kill()
        srv.wait(10)
    time.sleep(2)
    assert alive(orphan2), "test setup: second orphan should be alive"

    # T6: --cleanup CLI (what the launcher runs) removes it without a server
    r = subprocess.run([sys.executable, SERVER, "--cleanup"], capture_output=True, text=True, env=ENV, timeout=120)
    show("--cleanup CLI", r.stdout.strip() + r.stderr.strip()[-200:], 400)
    time.sleep(1)
    print("after --cleanup -> alive:", alive(orphan2), "locks:", locks())
    assert not alive(orphan2) and not locks(), "T6 failed"
    print("\nALL CLEANUP CHECKS PASSED")


if __name__ == "__main__":
    main()
