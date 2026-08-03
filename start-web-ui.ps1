$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonPath = Join-Path $projectDir ".venv\Scripts\python.exe"
$pythonWindowPath = Join-Path $projectDir ".venv\Scripts\pythonw.exe"
$projectParent = Split-Path -Parent $projectDir
$registerProjectDir = $env:OPENAI_REGISTER_PROJECT_DIR
if (-not $registerProjectDir) {
    $registerCandidates = @(
        (Join-Path $projectParent "openai-register-paylink"),
        (Join-Path $projectParent "openai-register-paylink-ui-dist-20260706-README-deploy")
    )
    $registerProjectDir = $registerCandidates |
        Where-Object { Test-Path -LiteralPath (Join-Path $_ "app_backend.py") -PathType Leaf } |
        Select-Object -First 1
    if (-not $registerProjectDir) {
        $registerProjectDir = $registerCandidates[0]
    }
}
$registerPythonPath = $env:OPENAI_REGISTER_PYTHON
if (-not $registerPythonPath) {
    $registerPythonPath = Join-Path $registerProjectDir ".venv\Scripts\python.exe"
}
$logDir = Join-Path $env:LOCALAPPDATA "HideMyEmailGenerator"
$logFile = Join-Path $logDir "web-ui.log"
$errorLogFile = Join-Path $logDir "web-ui-error.log"

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "Python runtime not found: $pythonPath"
}
if (-not (Test-Path -LiteralPath $pythonWindowPath -PathType Leaf)) {
    throw "Background Python runtime not found: $pythonWindowPath"
}

$listener = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    exit 0
}

New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$env:OPENAI_REGISTER_PROJECT_DIR = $registerProjectDir
$env:OPENAI_REGISTER_PYTHON = $registerPythonPath
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"
Set-Location -LiteralPath $projectDir

Start-Process -FilePath $pythonWindowPath `
    -ArgumentList @(
        "-m",
        "hidemyemail_generator.webapp",
        "--host", "127.0.0.1",
        "--port", "8765",
        "--region", "china"
    ) `
    -WorkingDirectory $projectDir `
    -WindowStyle Hidden `
    -RedirectStandardOutput $logFile `
    -RedirectStandardError $errorLogFile | Out-Null
