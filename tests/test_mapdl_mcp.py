"""End-to-end localhost test of the MAPDL MCP server (own work dir, self-terminating).

Run:  .venv-mapdl/Scripts/python.exe tests/test_mapdl_mcp.py     (IDLE_TEST=1 for the idle-exit test)
"""
import asyncio, base64, os, socket, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

os.environ.setdefault("AWP_ROOT261", r"D:\Program Files\ANSYS\ANSYS Inc\v261")
SERVER = str(ROOT / "mapdl_mcp.py")
PORT = int(os.environ.get("TEST_PORT", "18802"))
IDLE_TEST = os.environ.get("IDLE_TEST") == "1"

import shutil
from pathlib import Path
TEST_WORK = ROOT / "work-test"
TEST_WORK.mkdir(exist_ok=True)
shutil.copy(ROOT / "tests" / "data" / "test_tube.step", TEST_WORK / "test_tube.step")
env = dict(os.environ, MAPDL_MCP_WORKDIR=str(TEST_WORK))
if IDLE_TEST:
    env.update(MAPDL_IDLE_MINUTES="0.1", MAPDL_IDLE_CHECK_SECONDS="2")
srv = subprocess.Popen([sys.executable, SERVER, "--host", "127.0.0.1", "--port", str(PORT)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)

PREP = """/PREP7
ET,1,BEAM188
MP,EX,1,70e9
MP,PRXY,1,0.3
SECTYPE,1,BEAM,CSOLID
SECDATA,0.005
K,1,0,0,0
K,2,1,0,0
L,1,2
LESIZE,ALL,,,20
LMESH,ALL
NSEL,S,LOC,X,1
CM,TIP,NODE
NSEL,S,LOC,X,0
CM,BASE,NODE
ALLSEL
FINISH"""

BAD = """/PREP7
K,10,2,0,0
LMESH,999
K,11,3,0,0"""

SOLU = """/SOLU
ANTYPE,STATIC
NLGEOM,ON
NSUBST,5,20,2
OUTRES,ALL,ALL
CMSEL,S,BASE
D,ALL,ALL,0
CMSEL,S,TIP
F,ALL,FY,-100
ALLSEL
FINISH"""

LOOP = """/PREP7
*DIM,RADII,ARRAY,3
*DO,I,1,3
RADII(I)=I*0.001
*ENDDO
FINISH"""


def show(label, r, n=600):
    txt = r.data if isinstance(r.data, str) else str(r.data)
    print(f"\n### {label}\n{txt[:n]}{'...' if len(txt) > n else ''}")
    return txt


async def main():
    from fastmcp import Client
    async with Client(f"http://127.0.0.1:{PORT}/mcp") as c:
        try:
            await body(c)
        finally:
            # always shut MAPDL down through the server itself (no process killing)
            print("\n### final mapdl_exit:", (await c.call_tool("mapdl_exit", {"save": False})).data)


async def body(c):
    if True:
        tools = await c.list_tools()
        print("tools:", len(tools), [t.name for t in tools])
        res = await c.list_resources()
        print("resources:", [str(r.uri) for r in res])
        prompts = await c.list_prompts()
        print("prompts:", [p.name for p in prompts])
        rr = await c.read_resource("mapdl://reference/elements")
        print("resource read OK, chars:", len(rr[0].text))
        pr = await c.get_prompt("nonlinear_static_workflow", {"goal": "test"})
        print("prompt OK, chars:", len(pr.messages[0].content.text))

        if IDLE_TEST:
            show("start", await c.call_tool("mapdl_start", {"nproc": 2}))
            show("input", await c.call_tool("mapdl_input", {"commands": PREP}))
            print("waiting for idle exit ...")
            st = ""
            for i in range(40):
                time.sleep(2)
                st = (await c.call_tool("mapdl_status", {})).data
                if "not running" in st:
                    print(f"idle exit observed after ~{2 * (i + 1)} s")
                    break
            show("status after idle", await c.call_tool("mapdl_status", {}))
            assert "not running" in st, "idle exit did not happen"
            g = show("get after idle (auto relaunch+resume)", await c.call_tool("mapdl_get", {"entity": "NODE", "entnum": 0, "item1": "COUNT"}))
            assert "21" in g, "model not restored after idle exit"
            show("status", await c.call_tool("mapdl_status", {}))
            show("exit", await c.call_tool("mapdl_exit", {}))
            return

        show("start", await c.call_tool("mapdl_start", {"nproc": 2}))
        show("status", await c.call_tool("mapdl_status", {}))
        t = show("input PREP7", await c.call_tool("mapdl_input", {"commands": PREP}))
        assert t.startswith("OK:"), "prep failed"
        t = show("input with error", await c.call_tool("mapdl_input", {"commands": BAD}))
        assert "FAILED at line 3" in t and "LMESH,999" in t, "error line not reported"
        t = show("input *DO block", await c.call_tool("mapdl_input", {"commands": LOOP}))
        assert t.startswith("OK:"), "block mode failed"
        show("get_parameters", await c.call_tool("mapdl_get_parameters", {}))
        show("set_parameters", await c.call_tool("mapdl_set_parameters", {"params": {"E_MOD": 70e9, "LEN": 1.0}}))
        show("get_parameters named", await c.call_tool("mapdl_get_parameters", {"names": ["E_MOD", "LEN", "RADII"]}))
        show("mapdl_get", await c.call_tool("mapdl_get", {"entity": "NODE", "entnum": 0, "item1": "COUNT"}))

        r = await c.call_tool("mapdl_plot", {"kind": "elements", "show_numbers": True})
        kinds = [type(x).__name__ for x in r.content]
        print("\n### plot elements content:", kinds, [getattr(x, 'text', '')[:80] for x in r.content if hasattr(x, 'text')])
        assert "ImageContent" in kinds, "no image returned"

        t = show("input SOLU", await c.call_tool("mapdl_input", {"commands": SOLU}))
        assert t.startswith("OK:")
        t = show("solve (wait 0)", await c.call_tool("mapdl_solve", {"wait_seconds": 0}))
        busy = show("run while solving", await c.call_tool("mapdl_run", {"command": "/PREP7"}), 200)
        for _ in range(60):
            s = (await c.call_tool("mapdl_solve_status", {})).data
            if "state=done" in s or "state=failed" in s:
                break
            time.sleep(1)
        t = show("solve_status final", await c.call_tool("mapdl_solve_status", {}), 900)
        assert "state=done" in t, "solve not done"
        show("status after solve", await c.call_tool("mapdl_status", {}))

        t = show("results tip UY", await c.call_tool("mapdl_get_results", {"item": "displacement", "component": "Y", "selection": "TIP"}))
        assert "nodes: 1" in t
        t = show("results reaction FY", await c.call_tool("mapdl_get_results", {"item": "reaction", "component": "FY"}))
        t = show("results all UY abs-sorted", await c.call_tool("mapdl_get_results", {"item": "displacement", "component": "Y", "sort": "abs", "max_rows": 3}))
        t = show("results nodes list", await c.call_tool("mapdl_get_results", {"item": "rotation", "component": "Z", "nodes": [1, 2]}))
        t = show("python theory check", await c.call_tool("mapdl_python", {"code":
            "import math\nE=70e9; r=0.005; I=math.pi*r**4/4\nresult = -100*1**3/(3*E*I)"}))

        r = await c.call_tool("mapdl_plot", {"kind": "displacement", "component": "NORM", "view": "xy"})
        kinds = [type(x).__name__ for x in r.content]
        print("\n### plot displacement content:", kinds, [getattr(x, 'text', '')[:80] for x in r.content if hasattr(x, 'text')])
        assert "ImageContent" in kinds
        r = await c.call_tool("mapdl_plot", {"kind": "stress_eqv"})
        print("### plot stress content:", [type(x).__name__ for x in r.content])

        show("list_files plots", await c.call_tool("mapdl_list_files", {"subdir": "plots"}))
        show("write_file", await c.call_tool("mapdl_write_file", {"name": "notes/readme.txt", "content": "hello\n" * 3}))
        show("read_file", await c.call_tool("mapdl_read_file", {"name": "notes/readme.txt"}))
        show("read_file log tail", await c.call_tool("mapdl_read_file", {"name": "logs/input_0002.log", "max_chars": 300, "tail": True}))
        b64 = (await c.call_tool("mapdl_download_file", {"name": "plots/plot_001_elements.png"})).data
        raw = base64.b64decode(b64)
        print("\n### download png bytes:", len(raw), "PNG header ok:", raw[:4] == b"\x89PNG")
        show("upload", await c.call_tool("mapdl_upload_file", {"name": "notes/copy.png", "data_base64": b64}))
        show("delete", await c.call_tool("mapdl_delete_file", {"name": "notes/copy.png"}))
        try:
            await c.call_tool("mapdl_read_file", {"name": "../mapdl_mcp.py"})
            raise AssertionError("path escape was not blocked")
        except Exception as e:
            print("\n### escape check blocked:", type(e).__name__, str(e)[:80])

        t = show("import_step", await c.call_tool("mapdl_import_step", {"name": "test_tube.step"}), 900)
        assert "imported" in t and "1 VOLU" in t, "step import failed"
        r = await c.call_tool("mapdl_plot", {"kind": "volumes"})
        print("### plot volumes content:", [type(x).__name__ for x in r.content])

        show("save", await c.call_tool("mapdl_save", {"name": "mytest"}))
        show("resume", await c.call_tool("mapdl_resume", {"name": "mytest"}))
        show("exit", await c.call_tool("mapdl_exit", {}))
        show("status after exit", await c.call_tool("mapdl_status", {}))


try:
    for _ in range(80):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", PORT)) == 0:
                break
        time.sleep(0.5)
    asyncio.run(main())
    print("\nALL CHECKS PASSED")
finally:
    srv.terminate()
    try:
        srv.wait(10)
    except Exception:
        srv.kill()
    print("server stopped")
