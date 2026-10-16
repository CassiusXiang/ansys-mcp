# ansys-mcp

English | [简体中文](README.zh-CN.md)

Exposes ANSYS 2026 R1 on a Windows workstation as three MCP servers, so agents on remote machines (for example Claude Code on a Mac) can drive it over Tailscale.

| Port | Server | Code | Purpose |
|---|---|---|---|
| 8802 | ansys-mapdl | `mapdl_mcp.py` (this repo, FastMCP + PyMAPDL) | Structural analysis (MAPDL / Mechanical APDL) |
| 8800 | ansys-fluent | Official `ansys-fluent-mcp` | Fluids |
| 8801 | ansys-cfx | Official `ansys-cfx-mcp` | Fluids |

All three servers bind only to the machine's Tailscale address (100.x.x.x), and the firewall only admits the Tailscale range. Do not bind them to 0.0.0.0 and expose them to the campus network: the servers have no authentication, and `mapdl_python` can run arbitrary Python.

## Layout

```
mapdl_mcp.py            MAPDL MCP server (23 tools, 4 resources, 2 prompts)
step_to_parasolid.py    STEP -> Parasolid conversion (run by the server as a subprocess in .venv-geom)
start-ansys-mcp.ps1     Launcher: cleans up leftovers, starts the three servers, prints the Mac-side commands, kills child processes on exit
ansysmcp.cmd            Command-line wrapper for the launcher
setup.ps1               Creates the three virtual environments and installs dependencies
requirements-*.txt      Dependencies for the three environments (split because of a protobuf version conflict)
tests/                  End-to-end tests (local loopback, separate work-test directory)
work/                   MAPDL working directory (created at runtime, git-ignored)
```

## Installation

Prerequisites: Windows, ANSYS 2026 R1 installed at `D:\Program Files\ANSYS\ANSYS Inc\v261` (if yours is elsewhere, change the `AWP_ROOT261` default in `start-ansys-mcp.ps1` and `mapdl_mcp.py`), Python 3.12, and Tailscale on both machines.

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1
```

Firewall (as administrator):

```powershell
netsh advfirewall firewall add rule name="ANSYS MCP" dir=in action=allow protocol=TCP localport=8800-8802 remoteip=100.64.0.0/10
```

To run `ansysmcp` from any directory, add the repo directory to PATH, or copy `ansysmcp.cmd` into a directory on PATH and change the path inside it to an absolute path.

## Running

On Windows:

```
ansysmcp
```

The script first stops any server and MAPDL process tree left over from a previous run, then starts the three servers and prints the commands to run on the Mac. Press Ctrl+C to stop; stopping also ends the solver child processes.

On the Mac (replace the IP with the Tailscale address the script prints):

```bash
claude mcp add --transport http ansys-mapdl  http://100.74.135.67:8802/mcp
claude mcp add --transport http ansys-fluent http://100.74.135.67:8800/mcp
claude mcp add --transport http ansys-cfx    http://100.74.135.67:8801/mcp
```

## MAPDL server tools

Session: `mapdl_start` (`restart=True` kills the whole process tree and restarts), `mapdl_status` (does not take the lock), `mapdl_exit`, `mapdl_cleanup`, `mapdl_save`, `mapdl_resume`. Any tool starts MAPDL automatically; after 30 idle minutes the server saves and exits to release the license, and the next call resumes automatically.

Modeling and solving: `mapdl_run` (a single APDL command), `mapdl_input` (multi-line APDL, executed line by line with the failing line reported; blocks containing `*DO`/`*IF` go through /INPUT), `mapdl_solve` (background solve with streamed log), `mapdl_solve_status` (progress, without taking the lock), `mapdl_python` (PyMAPDL script with `mapdl` and `np` in scope).

Results: `mapdl_get_results` (displacement, rotation, stress, strain, temperature, reaction forces; filter by component or node; CSV table), `mapdl_get_parameters`, `mapdl_set_parameters`, `mapdl_get` (*GET), `mapdl_plot` (mesh, geometry, deformed shape, stress contours; returned as a PNG image).

Files: `mapdl_list_files`, `mapdl_read_file`, `mapdl_write_file`, `mapdl_download_file`, `mapdl_upload_file` (base64, 50 MB limit), `mapdl_delete_file`, `mapdl_import_step`.

Resources: `mapdl://reference/workflow`, `mapdl://reference/elements`, `mapdl://workdir/files`, `mapdl://status`. Prompts: `nonlinear_static_workflow`, `verify_cantilever`.

## Troubleshooting

- If a solve hangs, tools keep returning BUSY, or startup fails with a lock-file error: call `mapdl_cleanup(force=True)` remotely. It ends the whole MAPDL process tree in this server's working directory, deletes lock files, and resets state; the next call restarts MAPDL and restores the model from `autosave.db`.
- On the workstation: `.venv-mapdl\Scripts\python.exe mapdl_mcp.py --cleanup`. The server does not need to be running.
- The server also cleans up orphaned processes from a previous run when it starts; exit, restart, and idle exit all save before killing the process tree.
- Cleanup only ends processes whose executable is under the ANSYS install directory and whose working directory is this server's work directory, so other ANSYS instances are left alone.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `AWP_ROOT261` | `D:\Program Files\ANSYS\ANSYS Inc\v261` | ANSYS install directory |
| `MAPDL_MCP_WORKDIR` | `./work` | MAPDL working directory |
| `MAPDL_NPROC` | 4 | Number of solver cores (multiple cores need an HPC license) |
| `MAPDL_IDLE_MINUTES` | 30 | Idle time before automatic exit; 0 disables it |
| `MAPDL_BUSY_TIMEOUT` | 5 | Seconds to wait for the lock before returning BUSY |
| `P_SCHEMA` | Auto-detected | Parasolid schema directory, needed for STEP import |

## Known limitations

- STEP import converts to Parasolid through the Geometry Service, then to .anf with ANSYS's bundled `ac4para.exe`, and finally reads it with `/INPUT`. MAPDL's own `~PARAIN` cannot launch the translator outside Workbench. Each import takes about 1 to 2 minutes, mostly Geometry Service startup. Geometry keeps the units of the CAD file.
- Streaming solve output relies on PyMAPDL's internal gRPC interface; if that breaks, the server falls back to a blocking SOLVE, so you just lose live progress.
- No authentication, and no start on boot.

## Tests

```powershell
.venv-mapdl\Scripts\python.exe tests\test_mapdl_mcp.py     # modeling, error location, solve, results, plotting, files, STEP import
.venv-mapdl\Scripts\python.exe tests\test_cleanup.py       # tree kill on exit/restart, forced cleanup, orphan cleanup, --cleanup
```

Both tests start a temporary server on the loopback address with a separate `work-test` directory, and they really launch MAPDL (a license is required).
