"""MCP server exposing Ansys MAPDL (via PyMAPDL) to remote agents.

Features
- One MAPDL session, serialised with a lock; busy calls return quickly instead of hanging.
- Condensed output: errors/warnings extracted, full logs kept in work/logs/.
- Line-by-line APDL execution that reports exactly which command failed.
- Background SOLVE with a lock-free progress/status tool.
- Auto-save after each successful block, resume after crash or idle exit.
- Idle auto-exit (releases the license), auto-relaunch + resume on next call.
- Structured result extraction, parameter get/set, *GET queries.
- PNG plots (mesh, geometry, deformation, stress) returned as images.
- Text + binary (base64) file transfer, STEP import via Geometry Service -> Parasolid -> ~PARAIN.
- MCP resources (APDL cheat sheets) and prompts (workflows).
"""
from __future__ import annotations

import argparse
import atexit
import base64
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

import psutil
from fastmcp import FastMCP
from fastmcp.utilities.types import Image

# --------------------------------------------------------------------------- config
HERE = Path(__file__).resolve().parent
WORK_DIR = Path(os.environ.get("MAPDL_MCP_WORKDIR", HERE / "work")).resolve()
LOG_DIR = WORK_DIR / "logs"
PLOT_DIR = WORK_DIR / "plots"
for _d in (WORK_DIR, LOG_DIR, PLOT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

AWP_ROOT = os.environ.setdefault("AWP_ROOT261", r"D:\Program Files\ANSYS\ANSYS Inc\v261")
DEFAULT_NPROC = int(os.environ.get("MAPDL_NPROC", "4"))
IDLE_MINUTES = float(os.environ.get("MAPDL_IDLE_MINUTES", "30"))
IDLE_CHECK_SECONDS = float(os.environ.get("MAPDL_IDLE_CHECK_SECONDS", "60"))
BUSY_TIMEOUT = float(os.environ.get("MAPDL_BUSY_TIMEOUT", "5"))
AUTOSAVE_JOB = "autosave"
MAX_TRANSFER = 50 * 1024 * 1024
GEOM_PYTHON = HERE / ".venv-geom" / "Scripts" / "python.exe"
STEP_SCRIPT = HERE / "step_to_parasolid.py"
AC4PARA = Path(AWP_ROOT) / "ansys" / "ac4" / "bin" / "para" / "winx64" / "ac4para.exe"
PID_FILE = WORK_DIR / ".mapdl_pids.json"  # PIDs of the MAPDL tree this server launched (survives a server restart)

# ~PARAIN runs the external translator ac4para.exe, which needs the Parasolid schema directory.
# Workbench sets P_SCHEMA for it; a bare PyMAPDL launch does not, so set it here (MAPDL inherits it).
if "P_SCHEMA" not in os.environ:
    _schemas = sorted(Path(AWP_ROOT).glob("commonfiles/CAD/Siemens/Parasolid*/winx64/schema"))
    if _schemas:
        os.environ["P_SCHEMA"] = str(_schemas[-1])

os.environ.setdefault("PYVISTA_OFF_SCREEN", "true")

# --------------------------------------------------------------------------- state


class _State:
    mapdl = None
    version: str | None = None
    nproc: int = 0
    started_at: datetime | None = None
    last_activity: float = time.time()
    busy_with: str | None = None
    idle_exited: bool = False
    resumed_from: str | None = None
    globals: dict = {}
    input_seq: int = 0
    plot_seq: int = 0
    solve: dict = {"state": "idle"}


S = _State()
_lock = threading.Lock()  # plain lock: the solve worker acquires/releases it in its own thread


def _ensure(nproc: int | None = None):
    """Return a live MAPDL session, launching (and resuming autosave) if needed."""
    if S.mapdl is not None:
        try:
            if S.mapdl.is_alive:
                return S.mapdl
        except Exception:
            pass
        S.mapdl = None
    from ansys.mapdl.core import launch_mapdl
    import numpy as np

    S.nproc = nproc or S.nproc or DEFAULT_NPROC
    kw = dict(run_location=str(WORK_DIR), override=True, loglevel="ERROR", nproc=S.nproc, print_com=False)
    try:
        S.mapdl = launch_mapdl(**kw)
    except Exception as e:
        msg = str(e).lower()
        if "lock" not in msg and "another mapdl instance" not in msg:
            raise
        # A previous MAPDL tree (hung solve, abandoned restart, killed server) still owns the work dir:
        # remove it and retry once.
        _kill_mapdl_tree("stale instance blocking launch")
        _remove_stale_locks()
        S.mapdl = launch_mapdl(**kw)
    _record_pids(S.mapdl)
    S.version = str(S.mapdl.version)
    S.started_at = datetime.now()
    S.globals = {"mapdl": S.mapdl, "np": np, "WORK_DIR": WORK_DIR}
    S.resumed_from = None
    if S.idle_exited and (WORK_DIR / f"{AUTOSAVE_JOB}.db").exists():
        with contextlib.suppress(Exception):
            S.mapdl.resume(AUTOSAVE_JOB, "db")
            S.resumed_from = f"{AUTOSAVE_JOB}.db"
    S.idle_exited = False
    return S.mapdl


def _locked(tool: str, fn, need_mapdl: bool = True):
    """Run fn(mapdl) under the session lock; return a BUSY message instead of blocking."""
    lk = _lock  # capture: mapdl_cleanup(force=True) may replace the global lock while a hung call holds this one
    if not lk.acquire(timeout=BUSY_TIMEOUT):
        return (f"BUSY: MAPDL is currently used by `{S.busy_with}`. "
                "If a solve is running, poll mapdl_solve_status; otherwise retry shortly. "
                "If that call is hung, mapdl_cleanup(force=True) clears it.")
    S.busy_with = tool
    S.last_activity = time.time()
    try:
        m = _ensure() if need_mapdl else None
        return fn(m)
    except Exception as e:  # noqa: BLE001
        return f"ERROR in {tool}: {type(e).__name__}: {str(e)[:2000]}"
    finally:
        S.last_activity = time.time()
        S.busy_with = None
        lk.release()


# --------------------------------------------------------------------------- process-tree management
def _ansys_root() -> str:
    return str(Path(AWP_ROOT).resolve()).lower()


def _is_ansys_proc(p: psutil.Process) -> bool:
    """True only for processes whose executable lives inside the ANSYS installation."""
    try:
        return (p.exe() or "").lower().startswith(_ansys_root())
    except Exception:
        return False


def _find_mapdl_processes() -> dict[int, str]:
    """MAPDL processes that belong to THIS server's working directory, with their children.
    Sources: (a) PIDs recorded in PID_FILE at launch, (b) PIDs listed in MAPDL's own cleanup-ansys-*.bat
    files in the work dir, (c) ANSYS-installation executables whose current directory is the work dir.
    Only processes whose executable is inside the ANSYS installation are ever returned."""
    cands: set[int] = set()
    with contextlib.suppress(Exception):
        cands.update(int(x) for x in json.loads(PID_FILE.read_text()).get("pids", []))
    for bat in WORK_DIR.glob("cleanup-ansys-*.bat"):
        with contextlib.suppress(Exception):
            cands.update(int(x) for x in re.findall(r"/pid\s+(\d+)", bat.read_text(errors="replace"), re.I))
    wd = str(WORK_DIR).lower()
    for p in psutil.process_iter(["pid"]):
        try:
            if _is_ansys_proc(p) and p.cwd().lower() == wd:
                cands.add(p.pid)
        except Exception:
            continue
    found: dict[int, str] = {}
    for pid in cands:
        try:
            root = psutil.Process(pid)
        except psutil.NoSuchProcess:
            continue
        for q in [root] + root.children(recursive=True):
            if _is_ansys_proc(q):
                with contextlib.suppress(Exception):
                    found[q.pid] = q.name()
    return found


def _kill_mapdl_tree(reason: str) -> str:
    """Terminate every process returned by _find_mapdl_processes and wait for them to disappear."""
    procs = _find_mapdl_processes()
    if not procs:
        return f"no MAPDL processes to clean up ({reason})"
    handles, failed = [], []
    for pid, name in procs.items():
        try:
            p = psutil.Process(pid)
            p.kill()
            handles.append(p)
        except psutil.NoSuchProcess:
            pass
        except Exception as e:
            failed.append(f"{name}:{pid} ({type(e).__name__})")
    gone, alive = psutil.wait_procs(handles, timeout=20)
    for bat in WORK_DIR.glob("cleanup-ansys-*.bat"):  # MAPDL's own kill lists; stale once the tree is gone
        with contextlib.suppress(Exception):
            bat.unlink()
    rep = f"killed {len(gone)} MAPDL process(es) [{reason}]: " + ", ".join(f"{procs[p.pid]}:{p.pid}" for p in gone)
    if alive:
        rep += "; STILL ALIVE: " + ", ".join(f"{procs[p.pid]}:{p.pid}" for p in alive)
    if failed:
        rep += "; failed: " + ", ".join(failed)
    return rep


def _remove_stale_locks() -> str:
    removed, stuck = [], []
    for lock in list(WORK_DIR.glob("*.lock")):
        try:
            lock.unlink()
            removed.append(lock.name)
        except Exception:
            stuck.append(lock.name)
    with contextlib.suppress(Exception):
        PID_FILE.unlink()
    out = []
    if removed:
        out.append("removed " + ", ".join(removed))
    if stuck:
        out.append("could not remove " + ", ".join(stuck) + " (still held by a process)")
    return "; ".join(out)


def _record_pids(m) -> None:
    """Persist the PIDs of the MAPDL tree so a later server process can still clean it up."""
    pids: set[int] = set()
    proc = getattr(m, "_mapdl_process", None)
    if getattr(proc, "pid", None):
        pids.add(int(proc.pid))
    with contextlib.suppress(Exception):
        pids.update(int(x) for x in (getattr(m, "_pids", None) or []) if x)
    for pid in list(pids):
        with contextlib.suppress(Exception):
            pids.update(c.pid for c in psutil.Process(pid).children(recursive=True))
    with contextlib.suppress(Exception):
        PID_FILE.write_text(json.dumps({"pids": sorted(pids), "port": getattr(m, "port", None),
                                        "started": datetime.now().isoformat(timespec="seconds")}))


def _run_with_timeout(fn, timeout: float, label: str) -> str:
    """Run fn in a helper thread; give up (but leave the thread) after timeout seconds."""
    box: dict = {}

    def runner():
        try:
            box["ret"] = fn()
            box["ok"] = True
        except Exception as e:  # noqa: BLE001
            box["err"] = f"{type(e).__name__}: {str(e)[:160]}"
    t = threading.Thread(target=runner, daemon=True, name=f"mapdl-{label}")
    t.start()
    t.join(timeout)
    if t.is_alive():
        return f"{label} timed out after {timeout:g} s"
    if not box.get("ok"):
        return f"{label} failed: {box.get('err')}"
    return f"{label} {box['ret']}" if isinstance(box.get("ret"), str) else f"{label} ok"


def _save_if_model(m) -> str | None:
    """SAVE to autosave.db, but never overwrite it with an empty model (fresh session, nothing built)."""
    if m.get_value("NODE", 0, "COUNT") + m.get_value("KP", 0, "COUNT") <= 0:
        return "skipped (empty model; autosave.db left untouched)"
    m.save(AUTOSAVE_JOB, "db")
    return None


def _shutdown_session(save: bool, reason: str, exit_timeout: float = 20) -> str:
    """Stop MAPDL for good: optional SAVE, polite exit (bounded), then kill the whole process tree,
    remove lock files and forget the session. Safe to call when the session is hung or already gone."""
    m, S.mapdl = S.mapdl, None
    S.globals = {}
    notes = []
    if m is not None:
        if save:
            notes.append(_run_with_timeout(lambda: _save_if_model(m), 120, "save"))
        notes.append(_run_with_timeout(lambda: m.exit(force=True), exit_timeout, "exit"))
    notes.append(_kill_mapdl_tree(reason))
    notes.append(_remove_stale_locks())
    return "; ".join(n for n in notes if n)


# --------------------------------------------------------------------------- output condensing
_MSG_RE = re.compile(r"^\s*\*\*\* (ERROR|WARNING|NOTE) \*\*\*")


def _extract_blocks(text: str) -> list[tuple[str, str]]:
    """Return [(kind, message)] for MAPDL *** ERROR *** / *** WARNING *** blocks."""
    lines = text.splitlines()
    out, i = [], 0
    while i < len(lines):
        mm = _MSG_RE.match(lines[i])
        if mm and mm.group(1) in ("ERROR", "WARNING"):
            body, j = [], i + 1
            while j < len(lines) and lines[j].strip() and not _MSG_RE.match(lines[j]):
                body.append(lines[j].strip())
                j += 1
            out.append((mm.group(1), " ".join(body) or lines[i].strip()))
            i = j
        else:
            i += 1
    return out


def _condense(text: str, max_chars: int = 3000, tail_n: int = 25) -> str:
    blocks = _extract_blocks(text)
    errors = [b for k, b in blocks if k == "ERROR"]
    warnings = [b for k, b in blocks if k == "WARNING"]
    parts = []
    if errors:
        parts.append(f"ERRORS ({len(errors)}):\n- " + "\n- ".join(errors[:10]))
    if warnings:
        parts.append(f"WARNINGS ({len(warnings)}):\n- " + "\n- ".join(warnings[:10]))
    nonblank = [l.rstrip() for l in text.splitlines() if l.strip()]
    if nonblank:
        parts.append("OUTPUT (last lines):\n" + "\n".join(nonblank[-tail_n:]))
    s = "\n\n".join(parts) if parts else "(no output)"
    return s if len(s) <= max_chars else s[:max_chars] + "\n...[truncated]"


def _first_error(text: str) -> str:
    errs = [b for k, b in _extract_blocks(text) if k == "ERROR"]
    if errs:
        return errs[0]
    return " ".join(text.split())[:600]


# --------------------------------------------------------------------------- server
mcp = FastMCP(
    "ansys-mapdl-mcp",
    instructions=(
        "Ansys MAPDL (Mechanical APDL) 2026 R1 on a Windows workstation, driven through PyMAPDL. "
        "Any tool auto-starts MAPDL. Typical flow: mapdl_input (APDL block for model + loads) -> "
        "mapdl_solve (background, poll mapdl_solve_status) -> mapdl_get_results / mapdl_plot / mapdl_python. "
        "mapdl_input runs line by line and tells you exactly which command failed; full logs are in work/logs. "
        "Successful blocks are auto-saved to autosave.db; MAPDL exits after idle time and resumes on the next call. "
        "Never restart while a solve runs; if a call is hung or a lock-file error appears, use mapdl_cleanup "
        "(force=True if BUSY) and then continue; the model comes back from autosave.db. "
        "Read resource mapdl://reference/workflow before building a model; mapdl://reference/elements for "
        "BEAM188/LINK180/MPC184 options. Units: use a consistent SI set (m, N, Pa, kg)."
    ),
)

_BLOCK_RE = re.compile(
    r"^\s*(\*DO|\*DOWHILE|\*IF|\*ELSE|\*ELSEIF|\*ENDIF|\*ENDDO|\*CREATE|\*END\b|\*REPEAT|\*USE|\*CYCLE|\*EXIT|\*RETURN)",
    re.I | re.M,
)


@mcp.tool
def mapdl_start(nproc: int = DEFAULT_NPROC, restart: bool = False, resume_autosave: bool = False,
                save: bool = True) -> str:
    """Start MAPDL (idempotent). restart=True saves (unless save=False), kills the whole MAPDL process
    tree and relaunches a fresh session. resume_autosave=True loads autosave.db afterwards.
    Returns BUSY while a solve runs; if that solve is hung, call mapdl_cleanup(force=True) first."""
    def go(_):
        notes = ""
        if restart:
            notes = " | restart: " + _shutdown_session(save=save and S.mapdl is not None, reason="restart")
            S.nproc = nproc
            S.idle_exited = False
        m = _ensure(nproc)
        msg = f"MAPDL {S.version} running, nproc={S.nproc}, workdir={WORK_DIR}{notes}"
        if resume_autosave:
            m.resume(AUTOSAVE_JOB, "db")
            S.resumed_from = f"{AUTOSAVE_JOB}.db"
        if S.resumed_from:
            msg += f" (resumed from {S.resumed_from})"
        return msg
    return _locked("mapdl_start", go, need_mapdl=False)


@mcp.tool
def mapdl_status() -> str:
    """Lock-free status: session, who is using it, solve state, idle timer, autosave. Safe to call any time."""
    now = time.time()
    if S.mapdl is None:
        sess = "MAPDL not running" + (" (stopped; the next call relaunches it and resumes autosave.db)" if S.idle_exited else "")
    else:
        alive = "running"
        lk = _lock
        if lk.acquire(blocking=False):
            try:
                alive = "running" if S.mapdl.is_alive else "DEAD (will relaunch on next call)"
            except Exception:
                alive = "unresponsive"
            finally:
                lk.release()
        sess = (f"MAPDL {S.version} {alive}, nproc={S.nproc}, started {S.started_at:%H:%M:%S}, "
                f"idle {int((now - S.last_activity) // 60)} min")
    procs = _find_mapdl_processes()
    locks = [p.name for p in WORK_DIR.glob("*.lock")]
    sess += f"\nMAPDL processes in work dir: {len(procs)}" + (f" ({', '.join(sorted(set(procs.values())))})" if procs else "")
    if S.mapdl is None and procs:
        sess += " <- orphaned; mapdl_cleanup will remove them"
    if locks:
        sess += f"\nlock files: {', '.join(locks)}"
    busy = f"busy with `{S.busy_with}`" if S.busy_with else "free"
    sv = S.solve
    solve = sv["state"]
    if sv.get("started"):
        solve += f" ({int((sv.get('finished') or now) - sv['started'])} s)"
    db = WORK_DIR / f"{AUTOSAVE_JOB}.db"
    auto = f"autosave.db {datetime.fromtimestamp(db.stat().st_mtime):%H:%M:%S}" if db.exists() else "no autosave.db"
    return (f"{sess}\nlock: {busy}\nsolve: {solve}\n{auto}\n"
            f"idle auto-exit after {IDLE_MINUTES:g} min\nworkdir: {WORK_DIR}")


@mcp.tool
def mapdl_run(command: str, max_chars: int = 3000) -> str:
    """Run ONE APDL command and return condensed output (errors/warnings first, then the last lines)."""
    def go(m):
        try:
            out = m.run(command) or ""
        except Exception as e:  # MapdlRuntimeError etc.
            return f"ERROR running `{command}`:\n{_first_error(str(e))}"
        return _condense(out, max_chars)
    return _locked("mapdl_run", go)


@mcp.tool
def mapdl_input(commands: str, stop_on_error: bool = True, autosave: bool = True, max_chars: int = 3000) -> str:
    """Run a multi-line APDL block. Plain command lists run line by line, so the reply names the exact
    failing command and how many ran before it. Blocks containing *DO/*IF/*CREATE run as one /INPUT file.
    Full output is saved to work/logs/input_NNNN.log. On success the database is saved to autosave.db."""
    def go(m):
        S.input_seq += 1
        log_path = LOG_DIR / f"input_{S.input_seq:04d}.log"
        rel_log = f"logs/{log_path.name}"
        chunks, warnings = [], []
        if _BLOCK_RE.search(commands):
            try:
                out = m.input_strings(commands) or ""
                failed = None
            except Exception as e:
                out, failed = str(e), ("block", "(see errors)", _first_error(str(e)))
            chunks.append(out)
            log_path.write_text("\n".join(chunks), errors="replace")
            blocks = _extract_blocks(out)
            errors = [b for k, b in blocks if k == "ERROR"]
            warnings = [b for k, b in blocks if k == "WARNING"]
            head = ("FAILED (block mode; MAPDL continues past errors inside /INPUT):\n- " + "\n- ".join(errors[:10])
                    if errors or failed else "OK: block executed via /INPUT (contains *DO/*IF/*CREATE)")
            tail = _condense(out, max_chars // 2)
            saved = _autosave(m) if autosave and not errors and not failed else ""
            w = f"\nWARNINGS ({len(warnings)}):\n- " + "\n- ".join(warnings[:10]) if warnings else ""
            return f"{head}{saved}\nlog: {rel_log}{w}\n\n{tail}"

        lines = commands.splitlines()
        total = sum(1 for l in lines if l.strip() and not l.strip().startswith("!"))
        executed, failed, last_out = 0, None, ""
        for idx, raw in enumerate(lines, 1):
            cmd = raw.strip()
            if not cmd or cmd.startswith("!"):
                continue
            try:
                out = m.run(cmd) or ""
            except Exception as e:
                out = str(e)
                failed = (idx, cmd, _first_error(out))
                chunks.append(f"> {cmd}\n{out}")
                if stop_on_error:
                    break
                continue
            executed += 1
            last_out = out
            chunks.append(f"> {cmd}\n{out}" if out.strip() else f"> {cmd}")
            warnings += [b for k, b in _extract_blocks(out) if k == "WARNING"]
        log_path.write_text("\n".join(chunks), errors="replace")
        w = f"\nWARNINGS ({len(warnings)}):\n- " + "\n- ".join(warnings[:10]) if warnings else ""
        if failed:
            idx, cmd, err = failed
            skipped = total - executed - 1 if stop_on_error else 0
            msg = (f"FAILED at line {idx}: `{cmd}`\n{err}\n"
                   f"{executed} command(s) executed before the failure"
                   + (f"; {skipped} skipped (stop_on_error=True)." if stop_on_error
                      else "; continued past errors (stop_on_error=False)."))
            return f"{msg}\nlog: {rel_log}{w}"
        saved = _autosave(m) if autosave else ""
        tail = _condense(last_out, max_chars // 2, tail_n=12) if last_out.strip() else "(last command produced no output)"
        return f"OK: {executed}/{total} commands executed{saved}\nlog: {rel_log}{w}\n\n{tail}"
    return _locked("mapdl_input", go)


def _autosave(m) -> str:
    try:
        m.save(AUTOSAVE_JOB, "db")
        return f" | autosaved -> {AUTOSAVE_JOB}.db"
    except Exception as e:
        return f" | autosave failed: {str(e)[:120]}"


@mcp.tool
def mapdl_save(name: str = AUTOSAVE_JOB) -> str:
    """SAVE the current database to <name>.db in the working directory."""
    return _locked("mapdl_save", lambda m: (m.save(name, "db"), f"saved {name}.db")[1])


@mcp.tool
def mapdl_resume(name: str = AUTOSAVE_JOB) -> str:
    """RESUME a database (<name>.db) into the running session, replacing the current model."""
    def go(m):
        m.resume(name, "db")
        S.resumed_from = f"{name}.db"
        return f"resumed {name}.db"
    return _locked("mapdl_resume", go)


# --------------------------------------------------------------------------- background solve
_SOLVE_STEP_RE = re.compile(r"LOAD STEP\s+(\d+)\s+SUBSTEP\s+(\d+)\s+COMPLETED\.\s+CUM ITER =\s+(\d+)")
_CONV_RE = re.compile(r"(FORCE|DISP|MOMENT|ROT)\s+CONVERGENCE VALUE\s*=\s*([\d.E+-]+)\s+CRITERION=\s*([\d.E+-]+)")


def _solve_progress(text: str) -> str:
    steps = _SOLVE_STEP_RE.findall(text)
    conv = _CONV_RE.findall(text)
    converged = text.count("SOLUTION CONVERGED")
    bits = []
    if steps:
        ls, ss, it = steps[-1]
        bits.append(f"last completed: load step {ls} substep {ss} (cum. iterations {it}); substeps completed: {len(steps)}")
    if conv:
        k, v, c = conv[-1]
        bits.append(f"last {k.lower()} convergence value {v} vs criterion {c}")
    if converged:
        bits.append(f"'SOLUTION CONVERGED' seen {converged} time(s)")
    errs = [b for k, b in _extract_blocks(text) if k == "ERROR"]
    if errs:
        bits.append("ERRORS:\n- " + "\n- ".join(errs[:5]))
    m = re.findall(r"ELAPSED TIME =\s+([\d.]+)", text)
    if m and "ROUTINE COMPLETED" in text:
        bits.append(f"solver routine completed, elapsed {m[-1]} s")
    return "\n".join(bits) or "(no progress lines yet)"


def _stream_solve(m, log: Path) -> str | None:
    """Run SOLVE through PyMAPDL's streaming gRPC call, appending output to `log` as it arrives
    so mapdl_solve_status can show live progress. Returns None if streaming is unavailable."""
    try:
        from ansys.mapdl.core import mapdl_grpc
        pb_types = mapdl_grpc.pb_types
        request = pb_types.CmdRequest(command="SOLVE")
        metadata = [("time_step_stream", str(m._get_time_step_stream()))]
        stream = m._stub.SendCommandS(request, metadata=metadata)
    except Exception:
        return None
    chunks = []
    try:
        with log.open("w", errors="replace") as f:
            for item in stream:
                out = "\n".join(item.cmdout)
                chunks.append(out)
                f.write(out + "\n")
                f.flush()
    except Exception as e:  # transport error mid-solve: report it as an error block
        chunks.append(f"\n *** ERROR ***\n streaming interrupted: {type(e).__name__}: {str(e)[:300]}\n")
        with contextlib.suppress(Exception):
            log.write_text("\n".join(chunks), errors="replace")
    return "\n".join(chunks)


def _solve_worker(log_name: str, started: threading.Event):
    sv = S.solve
    lk = _lock  # capture (see _locked)
    if not lk.acquire(timeout=BUSY_TIMEOUT):
        sv.update(state="failed", error=f"MAPDL busy with {S.busy_with}", finished=time.time())
        started.set()
        return
    S.busy_with = "mapdl_solve"
    S.last_activity = time.time()
    log = WORK_DIR / log_name
    try:
        m = _ensure()
        if log.exists():
            log.unlink()
        sv["state"] = "running"
        started.set()
        m.run("/SOLU")  # SOLVE is only valid inside the solution processor
        text = _stream_solve(m, log)
        if text is None:  # streaming unavailable -> blocking fallback
            try:
                text = m.run("SOLVE") or ""
            except Exception as e:
                text = str(e)
            log.write_text(text, errors="replace")
        errs = [b for k, b in _extract_blocks(text) if k == "ERROR"]
        sv.update(state="failed" if errs else "done", finished=time.time(),
                  error="; ".join(errs[:3]) or None, summary=_solve_progress(text))
        if not errs:
            with contextlib.suppress(Exception):
                m.save(AUTOSAVE_JOB, "db")
    except Exception as e:  # noqa: BLE001
        sv.update(state="failed", finished=time.time(), error=f"{type(e).__name__}: {str(e)[:800]}")
        started.set()
    finally:
        S.last_activity = time.time()
        if S.busy_with == "mapdl_solve":
            S.busy_with = None
        with contextlib.suppress(RuntimeError):
            lk.release()


@mcp.tool
def mapdl_solve(wait_seconds: float = 30, log_name: str = "solve_log.txt") -> str:
    """Issue SOLVE in the background (you must already be in /SOLU with loads applied).
    Waits up to wait_seconds for completion and returns the summary if done; otherwise returns
    immediately with state=running. Poll mapdl_solve_status (lock-free) for progress.
    Other tools return BUSY while the solve runs. Output is written to work/<log_name>."""
    if S.solve.get("state") == "running":
        return "A solve is already running. Poll mapdl_solve_status."
    S.solve = {"state": "starting", "started": time.time(), "finished": None, "error": None, "log": log_name}
    started = threading.Event()
    t = threading.Thread(target=_solve_worker, args=(log_name, started), daemon=True)
    t.start()
    started.wait(timeout=BUSY_TIMEOUT + 60)
    t.join(timeout=max(0.0, wait_seconds))
    return mapdl_solve_status()


@mcp.tool
def mapdl_solve_status(tail_lines: int = 12) -> str:
    """Lock-free progress of the current/last background solve: state, elapsed time, last load step /
    substep / convergence line, errors, and the tail of the solve log."""
    sv = S.solve
    if sv.get("state") == "idle":
        return "No solve started in this session."
    now = time.time()
    elapsed = int((sv.get("finished") or now) - sv["started"])
    log = WORK_DIR / sv.get("log", "solve_log.txt")
    text = log.read_text(errors="replace") if log.exists() else ""
    head = f"state={sv['state']}  elapsed={elapsed}s  log=work/{log.name}"
    if sv.get("error"):
        head += f"\nerror: {sv['error']}"
    prog = sv.get("summary") or _solve_progress(text)
    nonblank = [l.rstrip() for l in text.splitlines() if l.strip()]
    tail = "\n".join(nonblank[-tail_lines:]) if nonblank else "(log empty so far)"
    return f"{head}\n{prog}\n--- log tail ---\n{tail}"


# --------------------------------------------------------------------------- python
@mcp.tool
def mapdl_python(code: str, max_chars: int = 6000) -> str:
    """Execute Python with PyMAPDL. In scope: `mapdl` (session), `np` (numpy), `WORK_DIR` (Path).
    Printed output is returned; assign to `result` to also return its repr. Globals persist between calls."""
    def go(_):
        buf = io.StringIO()
        S.globals.pop("result", None)
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                exec(compile(code, "<mapdl_python>", "exec"), S.globals)
        except Exception:
            buf.write(traceback.format_exc())
        out = buf.getvalue()
        if "result" in S.globals:
            out += ("\n" if out and not out.endswith("\n") else "") + "result = " + repr(S.globals["result"])
        out = out or "(no output)"
        return out if len(out) <= max_chars else out[:max_chars] + "\n...[truncated]"
    return _locked("mapdl_python", go)


# --------------------------------------------------------------------------- results & parameters
@contextlib.contextmanager
def _selection(m, selection: str, nodes: list[int] | None):
    """Temporarily restrict the selection to a component or node list; restore afterwards."""
    saved = []
    for ent, name in (("NODE", "_MCP_N"), ("ELEM", "_MCP_E")):
        with contextlib.suppress(Exception):  # CM fails on an empty selection (e.g. geometry-only model)
            if m.get_value(ent, 0, "COUNT") > 0:
                m.run(f"CM,{name},{ent}")
                saved.append(name)
    try:
        if nodes:
            m.nsel("NONE")
            for n in nodes:
                m.nsel("A", "NODE", "", n)
        elif selection:
            n_all = m.get_value("NODE", 0, "COUNT")
            e_all = m.get_value("ELEM", 0, "COUNT")
            m.cmsel("S", selection)
            n_sel = m.get_value("NODE", 0, "COUNT")
            e_sel = m.get_value("ELEM", 0, "COUNT")
            if n_sel == n_all and e_sel < e_all:  # element component -> take its nodes
                m.nsle("S")
        yield
    finally:
        for name in saved:
            with contextlib.suppress(Exception):
                m.cmsel("S", name)
                m.run(f"CMDELE,{name}")


def _set_result_set(m, load_step: int, substep: int) -> str:
    m.post1()
    if load_step:
        m.set(load_step, substep or 1)
    else:
        m.set("LAST")
    ls = int(m.get_value("ACTIVE", 0, "SET", "LSTP"))
    ss = int(m.get_value("ACTIVE", 0, "SET", "SBST"))
    t = m.get_value("ACTIVE", 0, "SET", "TIME")
    return f"load step {ls}, substep {ss}, time {t:g}"


@mcp.tool
def mapdl_get_results(item: str = "displacement", component: str = "NORM", selection: str = "",
                      nodes: list[int] | None = None, load_step: int = 0, substep: int = 0,
                      max_rows: int = 50, sort: str = "node") -> str:
    """Structured nodal results from the last (or given) result set.
    item: displacement | rotation | stress_eqv | stress_principal | stress_component | strain_eqv | temperature | reaction
    component: displacement/rotation X,Y,Z,NORM; stress_component X,Y,Z,XY,YZ,XZ; stress_principal 1,2,3; reaction FX,FY,FZ,MX,MY,MZ
    selection: node or element component name (CMSEL); nodes: explicit node list. Empty = all selected.
    sort: node | abs (largest magnitude first). Returns min/max/mean plus a CSV table (node,x,y,z,value)."""
    def go(m):
        import numpy as np
        info = _set_result_set(m, load_step, substep)
        with _selection(m, selection, nodes):
            pp = m.post_processing
            it, comp = item.lower(), component.upper()
            if it == "reaction":
                m.allsel()
                arr = np.asarray(m.get_array("NODE", 0, "RF", comp))
                nn = np.arange(1, len(arr) + 1)
                mask = arr != 0
                nnum, vals = nn[mask], arr[mask]
                xyz = (np.array([[m.get_value("NODE", int(n), "LOC", a) for a in "XYZ"] for n in nnum])
                       if len(nnum) else np.zeros((0, 3)))
                extra = f"sum of {comp} reactions = {vals.sum():.6g}\n"
            else:
                if it == "displacement":
                    vals = pp.nodal_displacement(comp)
                elif it == "rotation":
                    vals = pp.nodal_rotation(comp)
                elif it == "stress_eqv":
                    vals = pp.nodal_eqv_stress()
                elif it == "stress_principal":
                    vals = pp.nodal_principal_stress(component)
                elif it == "stress_component":
                    vals = pp.nodal_component_stress(comp)
                elif it == "strain_eqv":
                    vals = pp.nodal_elastic_eqv_strain()
                elif it == "temperature":
                    vals = pp.nodal_temperature()
                else:
                    return f"unknown item {item!r}"
                vals = np.asarray(vals)
                nnum = np.asarray(m.mesh.nnum)
                xyz = np.asarray(m.mesh.nodes)
                extra = ""
            if len(vals) == 0:
                return f"{info}: no nodes selected / no values"
            n = min(len(vals), len(nnum))
            vals, nnum, xyz = vals[:n], nnum[:n], xyz[:n]
            imin, imax = int(np.argmin(vals)), int(np.argmax(vals))
            head = (f"{item} {component}  set: {info}  nodes: {n}\n{extra}"
                    f"min {vals[imin]:.6g} @ node {nnum[imin]} | max {vals[imax]:.6g} @ node {nnum[imax]} "
                    f"| mean {vals.mean():.6g} | max|v| {np.abs(vals).max():.6g}")
            order = np.argsort(-np.abs(vals)) if sort == "abs" else np.argsort(nnum)
            rows = ["node,x,y,z,value"]
            for i in order[:max_rows]:
                rows.append(f"{nnum[i]},{xyz[i][0]:.6g},{xyz[i][1]:.6g},{xyz[i][2]:.6g},{vals[i]:.6g}")
            note = f"\n...({n - max_rows} more rows; raise max_rows or narrow selection)" if n > max_rows else ""
            return head + "\n" + "\n".join(rows) + note
    return _locked("mapdl_get_results", go)


@mcp.tool
def mapdl_get_parameters(names: list[str] | None = None) -> str:
    """Read APDL parameters. names=None lists all user parameters; arrays are summarised by shape."""
    def go(m):
        import numpy as np
        params = m.parameters
        keys = names or sorted(k for k in params.keys() if not k.startswith("_"))
        rows = []
        for k in keys:
            try:
                v = params[k]
            except Exception as e:
                rows.append(f"{k} = <error: {str(e)[:80]}>")
                continue
            if isinstance(v, np.ndarray):
                rows.append(f"{k} = array shape {v.shape}"
                            + (f" values {v.tolist()}" if v.size <= 12 else f" min {v.min():.6g} max {v.max():.6g}"))
            else:
                rows.append(f"{k} = {v!r}")
        return "\n".join(rows) or "(no user parameters)"
    return _locked("mapdl_get_parameters", go)


@mcp.tool
def mapdl_set_parameters(params: dict[str, float | int | str]) -> str:
    """Set APDL scalar parameters, e.g. {"E_MOD": 70e9, "RADIUS": 0.0005}. Usable in later APDL as E_MOD, RADIUS."""
    def go(m):
        for k, v in params.items():
            m.parameters[k] = v
        return "set " + ", ".join(f"{k}={v}" for k, v in params.items())
    return _locked("mapdl_set_parameters", go)


@mcp.tool
def mapdl_get(entity: str, entnum: int, item1: str, it1num: str | int = "", item2: str = "", it2num: str | int = "") -> str:
    """*GET query, e.g. (NODE,0,COUNT), (NODE,0,NUM,MAX), (ELEM,0,COUNT), (KP,2,LOC,X), (ACTIVE,0,SET,TIME)."""
    return _locked("mapdl_get", lambda m: repr(m.get_value(entity, entnum, item1, it1num, item2, it2num)))


# --------------------------------------------------------------------------- plots
@mcp.tool
def mapdl_plot(kind: str = "elements", component: str = "NORM", selection: str = "", view: str = "iso",
               show_edges: bool = True, show_numbers: bool = False, displacement_factor: float = 1.0,
               load_step: int = 0, substep: int = 0, width: int = 1000, height: int = 750, title: str = "") -> list:
    """Render a PNG and return it as an image (also saved to work/plots/).
    kind: elements | nodes | keypoints | lines | areas | volumes | displacement | stress_eqv | stress_principal | stress_component | temperature
    component: as in mapdl_get_results. view: iso | xy | xz | yz | yx | zx | zy.
    Result plots show the deformed shape scaled by displacement_factor (1 = true scale)."""
    def go(m):
        import pyvista as pv
        pv.OFF_SCREEN = True
        S.plot_seq += 1
        png = PLOT_DIR / f"plot_{S.plot_seq:03d}_{kind}.png"
        common = dict(savefig=str(png), off_screen=True, window_size=[width, height], cpos=view)
        if title:
            common["title"] = title
        k = kind.lower()
        with _selection(m, selection, None):
            if k in ("elements", "nodes", "keypoints", "lines", "areas", "volumes"):
                fn = {"elements": m.eplot, "nodes": m.nplot, "keypoints": m.kplot,
                      "lines": m.lplot, "areas": m.aplot, "volumes": m.vplot}[k]
                kw = dict(common)
                if k == "elements":
                    kw.update(show_edges=show_edges, show_node_numbering=show_numbers)
                elif k == "nodes":
                    kw.update(nnum=show_numbers)
                elif k == "lines":
                    kw.update(show_line_numbering=show_numbers)
                elif k == "keypoints":
                    kw.update(show_keypoint_numbering=show_numbers)
                else:
                    kw.update(show_lines=True)
                fn(**kw)
                return [f"plots/{png.name}", Image(path=png)]
            info = _set_result_set(m, load_step, substep)
            pp = m.post_processing
            rk = dict(common, show_displacement=True, displacement_factor=displacement_factor, show_edges=show_edges)
            if k == "displacement":
                pp.plot_nodal_displacement(component.upper(), **rk)
            elif k == "stress_eqv":
                pp.plot_nodal_eqv_stress(**rk)
            elif k == "stress_principal":
                pp.plot_nodal_principal_stress(component, **rk)
            elif k == "stress_component":
                pp.plot_nodal_component_stress(component.upper(), **rk)
            elif k == "temperature":
                pp.plot_nodal_temperature(**rk)
            else:
                return f"unknown kind {kind!r}"
            return [f"plots/{png.name} ({info})", Image(path=png)]
    r = _locked("mapdl_plot", go)
    return r if isinstance(r, list) else [r]


# --------------------------------------------------------------------------- files
def _safe_path(name: str) -> Path:
    p = (WORK_DIR / name).resolve()
    if WORK_DIR != p and WORK_DIR not in p.parents:
        raise ValueError("path escapes working directory")
    return p


@mcp.tool
def mapdl_list_files(pattern: str = "*", subdir: str = "") -> str:
    """List files in the working directory (or a subdir such as logs / plots) with sizes and times."""
    base = _safe_path(subdir) if subdir else WORK_DIR
    rows = []
    for p in sorted(base.glob(pattern)):
        rel = p.relative_to(WORK_DIR).as_posix()
        if p.is_file():
            st = p.stat()
            rows.append(f"{st.st_size:>12}  {datetime.fromtimestamp(st.st_mtime):%m-%d %H:%M}  {rel}")
        elif p.is_dir():
            rows.append(f"{'<dir>':>12}  {'':<11}  {rel}/")
    return "\n".join(rows) or "(empty)"


@mcp.tool
def mapdl_read_file(name: str, max_chars: int = 20000, tail: bool = False) -> str:
    """Read a text file from the working directory (logs/input_0003.log, solve_log.txt, a .csv ...).
    tail=True returns the last max_chars instead of the first."""
    p = _safe_path(name)
    if not p.is_file():
        return f"ERROR: {name} not found"
    data = p.read_text(errors="replace")
    if len(data) <= max_chars:
        return data
    if tail:
        return f"...[{len(data)} chars total, showing last {max_chars}]\n" + data[-max_chars:]
    return data[:max_chars] + f"\n...[truncated, {len(data)} chars total; use tail=True or raise max_chars]"


@mcp.tool
def mapdl_write_file(name: str, content: str) -> str:
    """Write a text file into the working directory (APDL macro .mac, input deck .inp, csv ...)."""
    p = _safe_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"wrote {len(content)} chars to {p.relative_to(WORK_DIR).as_posix()}"


@mcp.tool
def mapdl_download_file(name: str) -> str:
    """Download any file (binary ok: .db, .rst, .cdb, .x_t, .png) as base64. Limit 50 MB."""
    p = _safe_path(name)
    if not p.is_file():
        return f"ERROR: {name} not found"
    size = p.stat().st_size
    if size > MAX_TRANSFER:
        return f"ERROR: {name} is {size} bytes (> {MAX_TRANSFER}); fetch it another way"
    return base64.b64encode(p.read_bytes()).decode()


@mcp.tool
def mapdl_upload_file(name: str, data_base64: str) -> str:
    """Upload a binary file (STEP, Parasolid .x_t, .cdb, .db ...) into the working directory from base64."""
    p = _safe_path(name)
    raw = base64.b64decode(data_base64)
    if len(raw) > MAX_TRANSFER:
        return f"ERROR: {len(raw)} bytes exceeds limit {MAX_TRANSFER}"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(raw)
    return f"wrote {len(raw)} bytes to {p.relative_to(WORK_DIR).as_posix()}"


@mcp.tool
def mapdl_delete_file(name: str) -> str:
    """Delete a file from the working directory."""
    p = _safe_path(name)
    if not p.is_file():
        return f"ERROR: {name} not found"
    p.unlink()
    return f"deleted {name}"


@mcp.tool
def mapdl_import_step(name: str, entity: str = "SOLIDS", convert_only: bool = False) -> str:
    """Import a STEP file from the working directory into MAPDL geometry.
    Converts STEP -> Parasolid (.x_t) with the Ansys Geometry Service (takes ~1 min to start),
    then runs ~PARAIN in /PREP7. entity: SOLIDS | SURFACES | WIREFRAME | ALL.
    convert_only=True just produces the .x_t without importing."""
    src = _safe_path(name)
    if not src.is_file():
        return f"ERROR: {name} not found (upload it with mapdl_upload_file first)"
    if not GEOM_PYTHON.exists():
        return f"ERROR: geometry venv missing at {GEOM_PYTHON}"
    dst = src.with_suffix(".x_t")
    env = dict(os.environ, AWP_ROOT261=AWP_ROOT)
    r = subprocess.run([str(GEOM_PYTHON), "-I", str(STEP_SCRIPT), str(src), str(dst)],
                       capture_output=True, text=True, timeout=900, env=env)
    conv = "\n".join(l for l in (r.stdout + r.stderr).splitlines() if l.strip() and "warning" not in l.lower())[-1500:]
    if r.returncode != 0 or not dst.exists():
        return f"ERROR: STEP conversion failed (exit {r.returncode}):\n{conv}"
    if convert_only:
        return f"converted -> {dst.name}\n{conv}"

    # 2) Parasolid -> ANF (Ansys neutral file) with the Connection translator. MAPDL's own ~PARAIN cannot
    #    launch it outside the Workbench environment, so call ac4para.exe directly, then /INPUT the .anf.
    anf, plog = dst.with_suffix(".anf"), dst.with_suffix(".para_log")
    for p in (anf, plog):
        with contextlib.suppress(Exception):
            p.unlink()
    if not AC4PARA.exists():
        return f"ERROR: Parasolid translator not found at {AC4PARA}"
    env2 = dict(os.environ, ANSYS261_DIR=str(Path(AWP_ROOT) / "ansys"), ANSYS_SYSDIR="winx64")
    r2 = subprocess.run([str(AC4PARA), dst.name, anf.name, entity.upper()], cwd=str(WORK_DIR),
                        capture_output=True, text=True, timeout=600, env=env2)
    log_txt = plog.read_text(errors="replace") if plog.exists() else ""
    if not anf.exists() or anf.stat().st_size < 200:
        return (f"ERROR: Parasolid->ANF translation failed (exit {r2.returncode}).\n"
                f"translator output:\n{(r2.stdout + r2.stderr)[-1200:]}\n"
                f"translator log:\n{log_txt[-1200:]}\nP_SCHEMA={os.environ.get('P_SCHEMA', 'unset')}\nconversion log:\n{conv}")
    mu = re.search(r"Units:\s*(\S+)", log_txt)
    units = f"; translator reports units: {mu.group(1)}" if mu else ""

    def go(m):
        m.prep7()
        err = ""
        try:
            out = m.input(anf.name) or ""  # PyMAPDL's input(); a raw run('/INPUT,...') reads nothing in gRPC mode
        except Exception as e:  # PyMAPDL raises on warnings; judge by the resulting entity counts instead
            out = str(e)
            err = _first_error(out) or repr(e)[:400]
        counts = {e: int(m.get_value(e, 0, "COUNT")) for e in ("KP", "LINE", "AREA", "VOLU")}
        if sum(counts.values()) == 0:
            return (f"ERROR: {anf.name} was read but produced no geometry (counts {counts}).\n"
                    + (f"MAPDL said: {err}\n" if err else "") + f"translator log:\n{log_txt[-800:]}")
        return (f"imported {dst.name} -> {anf.name}: " + ", ".join(f"{v} {k}" for k, v in counts.items()) + units
                + "\nnote: geometry keeps the CAD file's units; if they are mm and your model is SI, scale with "
                  "VLSCALE,ALL,,,0.001,0.001,0.001,,0,1"
                + f"\n{_condense(out, 800, 6)}")
    return _locked("mapdl_import_step", go)


# --------------------------------------------------------------------------- resources & prompts
_REF_WORKFLOW = """# MAPDL workflow via this server

1. `mapdl_input` with a /PREP7 block: element types (ET), materials (MP), sections (SECTYPE/SECDATA),
   geometry (K/L/A/V or N/E), mesh (LESIZE/LMESH, ESIZE/VMESH), named components (CM) for later selection.
2. `mapdl_input` with a /SOLU block: ANTYPE, NLGEOM (if large deflection), NSUBST/AUTOTS, OUTRES,ALL,ALL,
   constraints (D/DK), loads (F/FK/SF/SFL/BF). Do NOT include SOLVE here.
3. `mapdl_solve` (background). Poll `mapdl_solve_status` until state=done. Check its convergence lines.
4. `mapdl_get_results` for numbers, `mapdl_plot` to look at mesh/deformation/stress, `mapdl_python` for custom post.
5. `mapdl_input` again for a new load case (/SOLU + new loads + `mapdl_solve`), or `mapdl_resume` to go back.

Tips
- Use one consistent unit system; SI (m, N, Pa, kg, s) is safest. CAD geometry (STEP/Parasolid) is usually mm:
  after mapdl_import_step scale with `VLSCALE,ALL,,,0.001,0.001,0.001,,0,1`.
- Name things: `NSEL,S,LOC,X,L` then `CM,TIP,NODE` so results can be fetched with selection="TIP".
- `mapdl_input` runs line by line; APDL `*DO`/`*IF` blocks are detected and run as one /INPUT file instead.
- Errors stop the block by default; the reply names the failing line. Full output: logs/input_NNNN.log.
- After a crash or idle exit, state comes back from autosave.db (last successful block / solve).
- Parameters: `mapdl_set_parameters({"L": 0.1})` then use `L` in APDL; `mapdl_get` for *GET queries.
"""

_REF_ELEMENTS = """# Element cheat sheet (structural, large deflection)

BEAM188 (3-D 2-node Timoshenko beam; large rotation OK with NLGEOM,ON)
  ET,1,BEAM188            KEYOPT,1,3,2 -> quadratic shape functions (better for coarse meshes)
  SECTYPE,1,BEAM,CSOLID   SECDATA,r                (solid circle, radius r)
  SECTYPE,1,BEAM,CTUBE    SECDATA,ri,ro            (tube)
  SECTYPE,1,BEAM,RECT     SECDATA,b,h
  Orientation: third node or SECOFFSET; for straight axial beams the default is fine.
  Results: nodal U/ROT via mapdl_get_results; section stresses via ETABLE/SMISC or mapdl_python.

LINK180 (3-D truss / cable; tension-only option)
  ET,2,LINK180
  SECTYPE,2,LINK          SECDATA,area            SECCONTROL,,1   -> tension only (0 = both, -1 compression only)
  Tendon force: apply as nodal F at the anchoring node, or shorten the cable with a negative thermal strain:
  MP,ALPX,2,1  + BF,node,TEMP,-strain  (strain = dL/L). INISTATE can also set an initial stress.

MPC184 (rigid link/beam, joints) - for rigid disks between backbone and tendon pass-through points
  ET,3,MPC184   KEYOPT,3,1,1  -> rigid beam (KEYOPT(1)=0 rigid link, 1 rigid beam)
  Alternative: CERIG,master,slave,ALL (rigid region, small rotation only) or CP for coupling.

SOLID185/186/187 for imported CAD solids: ET,4,SOLID187 (10-node tet) + ESIZE + VMESH after mapdl_import_step.
CONTA174/TARGE170 for tendon-disk sliding contact (only if friction matters).

Nonlinear solution settings (typical)
  /SOLU  ANTYPE,STATIC  NLGEOM,ON  NSUBST,20,200,5  AUTOTS,ON  OUTRES,ALL,ALL
  LNSRCH,ON (line search helps cables)   ARCLEN,ON (snap-through)   NEQIT,50
  CNVTOL,F,,0.005 if default force convergence is too strict with tension-only cables.
Materials (SI): NiTi superelastic austenite E~60-75e9 Pa nu 0.33 (linear approx); spring steel E 200e9 nu 0.3;
  PA/nylon E~2-3e9; Dyneema/UHMWPE tendon E~100e9 (treat tendons as inextensible if E*A >> backbone EI/L^2).
"""


@mcp.resource("mapdl://reference/workflow", mime_type="text/markdown")
def ref_workflow() -> str:
    """How to drive MAPDL through this server (read first)."""
    return _REF_WORKFLOW


@mcp.resource("mapdl://reference/elements", mime_type="text/markdown")
def ref_elements() -> str:
    """BEAM188 / LINK180 / MPC184 / solids options and nonlinear settings."""
    return _REF_ELEMENTS


@mcp.resource("mapdl://workdir/files", mime_type="text/plain")
def res_files() -> str:
    """Current listing of the working directory."""
    return mapdl_list_files()


@mcp.resource("mapdl://status", mime_type="text/plain")
def res_status() -> str:
    """Live session status (same as mapdl_status)."""
    return mapdl_status()


@mcp.prompt
def nonlinear_static_workflow(goal: str) -> str:
    """Step-by-step plan for a large-deflection static analysis on this server."""
    return (f"Goal: {goal}\n\n"
            "Work through these steps with the ansys-mapdl tools:\n"
            "1. Read resources mapdl://reference/workflow and mapdl://reference/elements.\n"
            "2. Decide units (SI) and write the /PREP7 block: ET/MP/SECTYPE, geometry, mesh, CM components "
            "(e.g. BASE, TIP). Send with mapdl_input; fix any failing line it reports.\n"
            "3. mapdl_plot kind=elements to check the mesh visually.\n"
            "4. /SOLU block: ANTYPE,STATIC NLGEOM,ON NSUBST AUTOTS OUTRES,ALL,ALL + constraints + loads (no SOLVE).\n"
            "5. mapdl_solve, poll mapdl_solve_status until done; if not converged, reduce load step size or add LNSRCH.\n"
            "6. mapdl_get_results (displacement NORM, selection=TIP), mapdl_plot kind=displacement.\n"
            "7. Sanity-check against a hand estimate (e.g. PL^3/3EI) before trusting the numbers.\n"
            "8. Summarise: inputs, convergence, key outputs, plots saved under work/plots.")


@mcp.prompt
def verify_cantilever() -> str:
    """Quick self-check: cantilever beam vs. PL^3/3EI."""
    return ("Build a 1 m BEAM188 cantilever (E=70e9, r=5 mm, 20 elements), tip load -100 N in Y, solve, "
            "and compare the tip deflection from mapdl_get_results with PL^3/(3EI). Expect < 1% difference "
            "(BEAM188 includes shear deformation). Report both numbers.")


# --------------------------------------------------------------------------- idle watchdog
def _idle_watch():
    while True:
        time.sleep(IDLE_CHECK_SECONDS)
        if S.mapdl is None or IDLE_MINUTES <= 0:
            continue
        if time.time() - S.last_activity < IDLE_MINUTES * 60:
            continue
        lk = _lock
        if not lk.acquire(blocking=False):
            continue
        try:
            if time.time() - S.last_activity < IDLE_MINUTES * 60 or S.mapdl is None:
                continue
            S.busy_with = "idle-exit"
            _shutdown_session(save=True, reason="idle")
            S.idle_exited = True
        finally:
            S.busy_with = None
            lk.release()


@mcp.tool
def mapdl_exit(save: bool = True) -> str:
    """Shut down MAPDL and release its license: SAVE (unless save=False), exit, then kill the whole
    MAPDL process tree and remove lock files, so nothing is left behind."""
    def go(_):
        was_running = S.mapdl is not None
        rep = _shutdown_session(save=save and was_running, reason="exit")
        S.idle_exited = False
        return ("MAPDL stopped" if was_running else "MAPDL was not running (cleaned up anyway)") + f": {rep}"
    return _locked("mapdl_exit", go, need_mapdl=False)


@mcp.tool
def mapdl_cleanup(force: bool = False) -> str:
    """Clear a stuck or orphaned MAPDL without needing a live session: kills every MAPDL process that
    belongs to this server's working directory (PIDs recorded at launch, MAPDL's own cleanup-ansys-*.bat
    lists, and ANSYS executables running in the work dir), removes stale lock files and resets the session
    state. The next tool call relaunches MAPDL and resumes autosave.db.
    force=True also works while another tool call holds the session (a hung solve or exit): that call's lock
    is abandoned. Use this when mapdl_start / mapdl_input report BUSY forever or a lock-file error."""
    global _lock
    note = ""
    lk = _lock
    if not lk.acquire(timeout=BUSY_TIMEOUT):
        if not force:
            return (f"BUSY: `{S.busy_with}` holds the session. If it is hung (no progress in "
                    "mapdl_solve_status), call mapdl_cleanup(force=True).")
        note = f"forced: abandoned the lock held by `{S.busy_with}`; "
        _lock = lk = threading.Lock()
        lk.acquire()
    try:
        S.busy_with = "mapdl_cleanup"
        rep = _shutdown_session(save=False, reason="cleanup", exit_timeout=5)
        if S.solve.get("state") in ("starting", "running"):
            S.solve.update(state="failed", finished=time.time(), error="cleared by mapdl_cleanup")
        S.idle_exited = True  # so the next call resumes autosave.db if it exists
        left = _find_mapdl_processes()
        tail = f"; WARNING {len(left)} process(es) still alive" if left else "; work dir clean"
        return f"cleanup done: {note}{rep}{tail}"
    finally:
        S.busy_with = None
        lk.release()


def _startup_cleanup() -> None:
    """A previous server run (killed launcher, abandoned restart) may have left a MAPDL tree in the work
    dir. It cannot be re-attached, so remove it before serving."""
    if _find_mapdl_processes():
        print("[mapdl-mcp] " + _kill_mapdl_tree("orphan from a previous server run"), file=sys.stderr)
    rep = _remove_stale_locks()
    if rep:
        print("[mapdl-mcp] " + rep, file=sys.stderr)


def _atexit_shutdown() -> None:
    if S.mapdl is not None or _find_mapdl_processes():
        print("[mapdl-mcp] server exiting: " + _shutdown_session(save=True, reason="server exit"), file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--transport", choices=["stdio", "http"], default="http")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8802)
    ap.add_argument("--cleanup", action="store_true",
                    help="kill any MAPDL tree in the work dir, remove lock files, and exit (no server)")
    a = ap.parse_args()
    if a.cleanup:
        print(_kill_mapdl_tree("--cleanup"))
        print(_remove_stale_locks() or "no lock files")
        return
    _startup_cleanup()
    atexit.register(_atexit_shutdown)
    threading.Thread(target=_idle_watch, daemon=True, name="mapdl-idle-watch").start()
    if a.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="http", host=a.host, port=a.port, path="/mcp")


if __name__ == "__main__":
    main()
