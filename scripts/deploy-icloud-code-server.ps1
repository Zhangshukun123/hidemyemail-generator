[CmdletBinding()]
param(
    [string]$SshHost = "aliyun-ecs",
    [string]$RemoteDirectory = "/opt/icloud-code-server",
    [ValidateRange(1, 65535)]
    [int]$RemotePort = 18767,
    [switch]$SkipLocalData
)

$ErrorActionPreference = "Stop"

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(Mandatory = $true)]
        [string[]]$ArgumentList
    )

    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed: $FilePath (exit code $LASTEXITCODE)"
    }
}

function Get-SharedToken {
    $token = [Environment]::GetEnvironmentVariable("HME_IMPORT_TOKEN", "Process")
    if (-not $token) {
        $token = [Environment]::GetEnvironmentVariable("HME_IMPORT_TOKEN", "User")
    }
    if (-not $token -or $token.Length -lt 32) {
        throw "HME_IMPORT_TOKEN must be configured with at least 32 characters."
    }
    return $token
}

$projectDirectory = Split-Path -Parent $PSScriptRoot
$requiredFiles = @(
    "Dockerfile",
    "pyproject.toml",
    "README.md",
    "README.zh-CN.md",
    "compose.code-server.yaml"
)
foreach ($name in $requiredFiles) {
    $path = Join-Path $projectDirectory $name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Missing deployment file: $path"
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $projectDirectory "src") -PathType Container)) {
    throw "Missing source directory: $projectDirectory\src"
}
if ($RemoteDirectory -notmatch '^/[A-Za-z0-9._/-]+$') {
    throw "RemoteDirectory must be an absolute Linux path using safe characters."
}

$token = Get-SharedToken
$tempDirectory = Join-Path ([System.IO.Path]::GetTempPath()) (
    "icloud-code-server-" + [Guid]::NewGuid().ToString("N")
)
New-Item -ItemType Directory -Path $tempDirectory | Out-Null
$envFile = Join-Path $tempDirectory ".env"
try {
    $envLines = @(
        "ACCOUNT_WORKBENCH_IMPORT_TOKEN=$token"
        "HIDEMYEMAIL_WEB_PASSWORD=$token"
        "HIDEMYEMAIL_INBOX_SYNC_INTERVAL=15"
        "HIDEMYEMAIL_REGION=china"
        "ICLOUD_CODE_SERVER_PORT=$RemotePort"
    )
    [System.IO.File]::WriteAllLines(
        $envFile,
        $envLines,
        [System.Text.UTF8Encoding]::new($false)
    )

    Write-Host "Checking SSH connection: $SshHost"
    try {
        Invoke-CheckedCommand -FilePath ssh -ArgumentList @(
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            $SshHost,
            "printf SSH_OK"
        )
    }
    catch {
        throw "SSH is unavailable for '$SshHost'. Restore SSH access before deploying. $($_.Exception.Message)"
    }
    Write-Host ""

    $quotedRemoteDirectory = "'$RemoteDirectory'"
    Invoke-CheckedCommand -FilePath ssh -ArgumentList @(
        $SshHost,
        "install -d -m 700 $quotedRemoteDirectory $quotedRemoteDirectory/data"
    )

    Write-Host "Uploading server source (Git history and logs are excluded)..."
    foreach ($name in $requiredFiles) {
        Invoke-CheckedCommand -FilePath scp -ArgumentList @(
            (Join-Path $projectDirectory $name),
            "${SshHost}:$RemoteDirectory/$name"
        )
    }
    Invoke-CheckedCommand -FilePath scp -ArgumentList @(
        "-r",
        (Join-Path $projectDirectory "src"),
        "${SshHost}:$RemoteDirectory/"
    )
    Invoke-CheckedCommand -FilePath scp -ArgumentList @(
        $envFile,
        "${SshHost}:$RemoteDirectory/.env"
    )

    if (-not $SkipLocalData) {
        foreach ($name in @("inbox_config.json", "cookies.txt", "hidemyemail.db")) {
            $path = Join-Path $projectDirectory $name
            if (Test-Path -LiteralPath $path -PathType Leaf) {
                Write-Host "Uploading runtime data: $name"
                Invoke-CheckedCommand -FilePath scp -ArgumentList @(
                    $path,
                    "${SshHost}:$RemoteDirectory/data/$name"
                )
            }
        }
    }

    $remoteCommand = @(
        "set -eu"
        "cd $quotedRemoteDirectory"
        "chown -R 10001:10001 data"
        "chmod 600 .env data/inbox_config.json data/cookies.txt data/hidemyemail.db 2>/dev/null || true"
        "docker compose -f compose.code-server.yaml up -d --build"
        "docker compose -f compose.code-server.yaml ps"
        "curl --fail --silent --show-error --retry 12 --retry-connrefused --retry-delay 2 http://127.0.0.1:$RemotePort/healthz"
    ) -join "; "
    Write-Host "Building and starting the server service..."
    Invoke-CheckedCommand -FilePath ssh -ArgumentList @($SshHost, $remoteCommand)
    Write-Host ""
    Write-Host "Deployment complete. The remote service only listens on 127.0.0.1:$RemotePort."
    Write-Host "Use the configured HTTPS reverse proxy: https://icloud-code.8-208-13-52.sslip.io/."
}
finally {
    if (Test-Path -LiteralPath $tempDirectory) {
        Remove-Item -LiteralPath $tempDirectory -Recurse -Force
    }
}
