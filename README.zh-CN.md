# ansys-mcp

[English](README.md) | 简体中文

把这台 Windows 工作站上的 ANSYS 2026 R1 暴露成三个 MCP 服务器，供远程机器上的 Agent（例如 Mac 上的 Claude Code）通过 Tailscale 调用。

| 端口 | 服务器 | 代码 | 用途 |
|---|---|---|---|
| 8802 | ansys-mapdl | `mapdl_mcp.py`（本仓库，FastMCP + PyMAPDL） | 结构分析（MAPDL / Mechanical APDL） |
| 8800 | ansys-fluent | 官方 `ansys-fluent-mcp` | 流体 |
| 8801 | ansys-cfx | 官方 `ansys-cfx-mcp` | 流体 |

三个服务器只绑定本机的 Tailscale 地址（100.x.x.x），防火墙只放行 Tailscale 网段，不要绑到 0.0.0.0 暴露到校园网：服务器没有鉴权，`mapdl_python` 可以执行任意 Python。

## 目录

```
mapdl_mcp.py            MAPDL MCP 服务器（23 个工具、4 个资源、2 个提示词）
step_to_parasolid.py    STEP -> Parasolid 转换（在 .venv-geom 里由服务器子进程调用）
start-ansys-mcp.ps1     启动脚本：清理残留、起三个服务器、打印 Mac 侧命令、退出时连子进程一起结束
ansysmcp.cmd            启动脚本的命令行包装
setup.ps1               建三个虚拟环境并安装依赖
requirements-*.txt      三个环境的依赖（分开是因为 protobuf 版本冲突）
tests/                  端到端测试（本机回环，独立 work-test 目录）
work/                   MAPDL 工作目录（运行时生成，已忽略）
```

## 安装

前提：Windows，ANSYS 2026 R1 装在 `D:\Program Files\ANSYS\ANSYS Inc\v261`（路径不同就改 `start-ansys-mcp.ps1` 和 `mapdl_mcp.py` 里的 `AWP_ROOT261` 默认值），Python 3.12，两台机器都装了 Tailscale。

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1
```

防火墙（管理员）：

```powershell
netsh advfirewall firewall add rule name="ANSYS MCP" dir=in action=allow protocol=TCP localport=8800-8802 remoteip=100.64.0.0/10
```

想在任何目录直接敲 `ansysmcp`，把仓库目录加进 PATH，或把 `ansysmcp.cmd` 复制到 PATH 里的目录并把里面的路径改成绝对路径。

## 运行

Windows：

```
ansysmcp
```

脚本会先结束上一次残留的服务器和 MAPDL 进程树，再启动三个服务器并打印 Mac 侧要执行的命令。Ctrl+C 停止，停止时连求解器子进程一起结束。

Mac（把 IP 换成脚本打印的 Tailscale 地址）：

```bash
claude mcp add --transport http ansys-mapdl  http://100.74.135.67:8802/mcp
claude mcp add --transport http ansys-fluent http://100.74.135.67:8800/mcp
claude mcp add --transport http ansys-cfx    http://100.74.135.67:8801/mcp
```

## MAPDL 服务器的工具

会话：`mapdl_start`（`restart=True` 杀整棵进程树再重启）、`mapdl_status`（不占锁）、`mapdl_exit`、`mapdl_cleanup`、`mapdl_save`、`mapdl_resume`。任何工具都会自动启动 MAPDL；空闲 30 分钟自动保存退出释放许可证，下次调用自动恢复。

建模与求解：`mapdl_run`（单条 APDL）、`mapdl_input`（多行 APDL，逐行执行并报告出错的行；含 `*DO`/`*IF` 的块走 /INPUT）、`mapdl_solve`（后台求解，流式日志）、`mapdl_solve_status`（不占锁的进度）、`mapdl_python`（PyMAPDL 脚本，`mapdl`、`np` 在作用域里）。

结果：`mapdl_get_results`（位移、转角、应力、应变、温度、支反力，按组件或节点筛选，CSV 表）、`mapdl_get_parameters`、`mapdl_set_parameters`、`mapdl_get`（*GET）、`mapdl_plot`（网格、几何、变形、应力云图，PNG 作为图片返回）。

文件：`mapdl_list_files`、`mapdl_read_file`、`mapdl_write_file`、`mapdl_download_file`、`mapdl_upload_file`（base64，50 MB 上限）、`mapdl_delete_file`、`mapdl_import_step`。

资源：`mapdl://reference/workflow`、`mapdl://reference/elements`、`mapdl://workdir/files`、`mapdl://status`。提示词：`nonlinear_static_workflow`、`verify_cantilever`。

## 卡住了怎么办

- 求解卡死、工具一直返回 BUSY、或启动报锁文件错误：远程调用 `mapdl_cleanup(force=True)`，它会结束本服务器工作目录里的整棵 MAPDL 进程树、删锁文件、重置状态，下一次调用自动重启并从 `autosave.db` 恢复模型。
- 在工作站上：`.venv-mapdl\Scripts\python.exe mapdl_mcp.py --cleanup`，不需要服务器在跑。
- 服务器自己在启动时也会清理上一次留下的孤儿进程；退出、重启、空闲退出都会先保存再杀树。
- 清理只会结束"可执行文件在 ANSYS 安装目录下、且工作目录是本服务器 work 目录"的进程，不会碰别的 ANSYS 实例。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `AWP_ROOT261` | `D:\Program Files\ANSYS\ANSYS Inc\v261` | ANSYS 安装目录 |
| `MAPDL_MCP_WORKDIR` | `./work` | MAPDL 工作目录 |
| `MAPDL_NPROC` | 4 | 求解核数（多核需要 HPC 许可证） |
| `MAPDL_IDLE_MINUTES` | 30 | 空闲多久自动退出，0 关闭 |
| `MAPDL_BUSY_TIMEOUT` | 5 | 等锁多少秒后返回 BUSY |
| `P_SCHEMA` | 自动探测 | Parasolid schema 目录，STEP 导入需要 |

## 已知限制

- STEP 导入走 Geometry Service 转 Parasolid、再用 ANSYS 自带的 `ac4para.exe` 转 .anf、最后 `/INPUT` 读入。MAPDL 自己的 `~PARAIN` 在 Workbench 环境之外拉不起翻译器。每次约 1 到 2 分钟，大头是 Geometry Service 启动。几何保留 CAD 文件的单位。
- 求解的流式输出用了 PyMAPDL 的内部 gRPC 接口；失效时自动退回阻塞式 SOLVE，只是看不到实时进度。
- 没有鉴权，没有开机自启。

## 测试

```powershell
.venv-mapdl\Scripts\python.exe tests\test_mapdl_mcp.py     # 建模、出错定位、求解、结果、画图、文件、STEP 导入
.venv-mapdl\Scripts\python.exe tests\test_cleanup.py       # 退出/重启杀树、强制清理、孤儿清理、--cleanup
```

两个测试都在本机回环地址起一个临时服务器，用独立的 `work-test` 目录，会真的启动 MAPDL（需要许可证）。
