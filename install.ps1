# openrouter-free-model-proxy installer for Windows.
#
# One-liner (installs to ~\openrouter-free-model-proxy and starts it in the
# background at every logon via Task Scheduler):
#
#   irm https://raw.githubusercontent.com/GoSlowPoke168/openrouter-free-model-proxy/main/install.ps1 | iex
#
# Env overrides (set before running, e.g. $env:NO_SERVICE = "1"):
#   DEST        where to install        (default: ~\openrouter-free-model-proxy)
#   REPO        source repo             (default: the GitHub repo above)
#   NO_SERVICE  "1" skips the scheduled task (just set up the venv)
$ErrorActionPreference = "Stop"

$Repo = if ($env:REPO) { $env:REPO } else { "https://github.com/GoSlowPoke168/openrouter-free-model-proxy" }
$Dest = if ($env:DEST) { $env:DEST } else { Join-Path $HOME "openrouter-free-model-proxy" }
$Name = "openrouter-free-model-proxy"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "git is required" }
# Prefer the py launcher: a bare "python" may be the Microsoft Store stub.
if (Get-Command py -ErrorAction SilentlyContinue) { $Py = @("py", "-3") }
elseif (Get-Command python -ErrorAction SilentlyContinue) { $Py = @("python") }
else { throw "python 3 is required (https://www.python.org/downloads/)" }

function Invoke-Checked([string[]]$Cmd) {
    & $Cmd[0] $Cmd[1..($Cmd.Length - 1)]
    if ($LASTEXITCODE -ne 0) { throw "failed: $($Cmd -join ' ')" }
}

# 1. Fetch (clone fresh, or update in place if already there).
if (Test-Path (Join-Path $Dest ".git")) {
    Write-Host ">> updating $Dest"
    Invoke-Checked @("git", "-C", $Dest, "pull", "--ff-only")
} else {
    Write-Host ">> cloning $Repo -> $Dest"
    Invoke-Checked @("git", "clone", "--depth", "1", $Repo, $Dest)
}

# 2. Virtualenv + deps.
Write-Host ">> creating venv + installing deps"
Invoke-Checked ($Py + @("-m", "venv", (Join-Path $Dest "venv")))
$VenvPy = Join-Path $Dest "venv\Scripts\python.exe"
Invoke-Checked @($VenvPy, "-m", "pip", "install", "-q", "--upgrade", "pip")
Invoke-Checked @($VenvPy, "-m", "pip", "install", "-q", "-r", (Join-Path $Dest "requirements.txt"))

# 3. Background task at logon (unless opted out).
if ($env:NO_SERVICE -eq "1") {
    Write-Host ">> NO_SERVICE=1 - skipping the scheduled task."
    Write-Host ">> start it yourself with: $VenvPy $(Join-Path $Dest 'proxy.py')"
} else {
    Write-Host ">> registering scheduled task '$Name'"
    # pythonw runs without a console window.
    $Action = New-ScheduledTaskAction -Execute (Join-Path $Dest "venv\Scripts\pythonw.exe") `
        -Argument "`"$(Join-Path $Dest 'proxy.py')`"" -WorkingDirectory $Dest
    $Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    # Zero time limit: the default would kill the proxy after 72 hours.
    $Settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $Name -Action $Action -Trigger $Trigger -Settings $Settings `
        -Description "Best free OpenRouter model, auto-selected" -Force | Out-Null
    Start-ScheduledTask -TaskName $Name
}

Write-Host ""
Write-Host "Done. Proxy will listen on http://127.0.0.1:8787 (base_url .../v1)."
Write-Host "Point any program there with model=`"auto`" and your own OpenRouter key."
