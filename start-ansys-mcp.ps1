# ANSYS MCP launcher: Fluent on 8800, CFX on 8801, MAPDL on 8802
# Binds to the Tailscale IP (100.x) when available, so only your own devices can reach it.
$env:AWP_ROOT261 = "D:\Program Files\ANSYS\ANSYS Inc\v261"
$venv  = "$PSScriptRoot\.venv\Scripts"
$venvM = "$PSScriptRoot\.venv-mapdl\Scripts"

$ip = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -like '100.*' } | Select-Object -First 1).IPAddress
if (-not $ip) {
    Write-Host "Tailscale IP not found, falling back to 0.0.0.0 (all interfaces)" -ForegroundColor Yellow
    $bind = "0.0.0.0"
    $ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.|192\.168\.56\.)' } | Select-Object -First 1).IPAddress
} else {
    $bind = $ip
}

# A previous run of THIS launcher may still own our ports (its venv python stub was stopped but the real
# server survived). Stop only processes whose command line points at this folder's servers.
foreach ($port in 8800, 8801, 8802) {
    $owner = (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1).OwningProcess
    if ($owner) {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId = $owner" -ErrorAction SilentlyContinue).CommandLine
        if ($cmd -and $cmd -like "*$PSScriptRoot*") {
            Write-Host "Stopping previous ANSYS MCP server on port $port (PID $owner) and its children" -ForegroundColor Yellow
            & taskkill /PID $owner /T /F 2>$null | Out-Null
            Start-Sleep -Seconds 1
        } else {
            Write-Host "Port $port is already used by PID $owner (not ours): $cmd" -ForegroundColor Red
        }
    }
}

# Remove any MAPDL process tree left behind by a previous run (the server does this too; belt and braces).
& "$venvM\python.exe" "$PSScriptRoot\mapdl_mcp.py" --cleanup

$fluent = Start-Process -PassThru -NoNewWindow "$venv\ansys-fluent-mcp.exe" -ArgumentList "--transport http --host $bind --port 8800"
$cfx    = Start-Process -PassThru -NoNewWindow "$venv\ansys-cfx-mcp.exe"    -ArgumentList "--transport http --host $bind --port 8801"
$mapdl  = Start-Process -PassThru -NoNewWindow "$venvM\python.exe"          -ArgumentList "`"$PSScriptRoot\mapdl_mcp.py`" --transport http --host $bind --port 8802"

Write-Host ""
Write-Host "ANSYS MCP is running on $bind. On the Mac run:" -ForegroundColor Green
Write-Host "  claude mcp add --transport http ansys-mapdl  http://${ip}:8802/mcp"
Write-Host "  claude mcp add --transport http ansys-fluent http://${ip}:8800/mcp"
Write-Host "  claude mcp add --transport http ansys-cfx    http://${ip}:8801/mcp"
Write-Host "Press Ctrl+C to stop." -ForegroundColor Yellow

try { Wait-Process -Id $fluent.Id, $cfx.Id, $mapdl.Id }
finally {
    # Kill each server WITH its children (taskkill /T), so MAPDL / Fluent / CFX solvers never outlive the servers.
    foreach ($p in @($fluent, $cfx, $mapdl)) {
        if ($p -and -not $p.HasExited) { & taskkill /PID $p.Id /T /F 2>$null | Out-Null }
    }
    & "$venvM\python.exe" "$PSScriptRoot\mapdl_mcp.py" --cleanup
}
