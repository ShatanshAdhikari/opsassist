# Starts the API, worker and web dev server in the background, logging to %LOCALAPPDATA%\opsassist.
# Prerequisites: the db container (docker compose up -d --wait db) and Ollama must already be running.
# Run once only: a second run starts a second worker. Stop the servers by ending their python and
# node processes.
$log  = "$env:LOCALAPPDATA\opsassist"
$root = Split-Path -Parent $PSScriptRoot
$venv = "$log\venv"
New-Item -ItemType Directory -Force $log | Out-Null

# Win32_Process.Create starts each process outside the caller's process tree and job object,
# so the servers survive when the launching shell exits.
$cmds = @(
    @{ dir = "$root\backend"; line = "cmd /c set UV_PROJECT_ENVIRONMENT=$venv&& python -m uv run uvicorn opsassist.asgi:app --port 8000 --no-access-log > `"$log\api.log`" 2>&1" },
    @{ dir = "$root\backend"; line = "cmd /c set UV_PROJECT_ENVIRONMENT=$venv&& python -m uv run opsassist worker > `"$log\worker.log`" 2>&1" },
    @{ dir = "$root\web";     line = "cmd /c npm run dev > `"$log\web.log`" 2>&1" }
)
foreach ($c in $cmds) {
    $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $c.line; CurrentDirectory = $c.dir }
    "pid $($r.ProcessId) rc $($r.ReturnValue): $($c.line.Substring(0, 60))"
}
