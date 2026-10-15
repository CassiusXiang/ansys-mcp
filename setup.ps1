# One-time setup: creates the three virtual environments and installs the pinned requirements.
# Three venvs are needed because PyMAPDL 0.74 requires protobuf 6 while the Fluent/CFX MCP
# servers and ansys-geometry-core require protobuf 7.
#
#   powershell -ExecutionPolicy Bypass -File setup.ps1
#
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

$plan = @(
    @(".venv",       "requirements-fluent.txt"),
    @(".venv-mapdl", "requirements-mapdl.txt"),
    @(".venv-geom",  "requirements-geom.txt")
)

foreach ($pair in $plan) {
    $venv = $pair[0]; $req = $pair[1]
    $py = "$root\$venv\Scripts\python.exe"
    if (-not (Test-Path $py)) {
        Write-Host "Creating $venv ..." -ForegroundColor Cyan
        python -m venv "$root\$venv"
    }
    Write-Host "Installing $req into $venv ..." -ForegroundColor Cyan
    & $py -m pip install --upgrade pip
    & $py -m pip install -r "$root\$req"
}

Write-Host ""
Write-Host "Done. Check the ANSYS path in start-ansys-mcp.ps1 (AWP_ROOT261), then run:  .\ansysmcp.cmd" -ForegroundColor Green
Write-Host "Firewall (admin):  netsh advfirewall firewall add rule name=""ANSYS MCP"" dir=in action=allow protocol=TCP localport=8800-8802 remoteip=100.64.0.0/10"
