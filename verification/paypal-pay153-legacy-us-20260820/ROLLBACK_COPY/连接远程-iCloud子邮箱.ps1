[CmdletBinding()]
param(
    [switch]$VerifyOnly
)

$ErrorActionPreference = 'Stop'

$bindAddress = Get-NetIPAddress -InterfaceAlias 'WLAN' -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike '169.254*' -and $_.AddressState -ne 'Duplicate' } |
    Select-Object -First 1 -ExpandProperty IPAddress

if (-not $bindAddress) {
    throw 'WLAN IPv4 address was not found.'
}

Write-Host "SSH bypasses Clash/Mihomo by binding WLAN: $bindAddress"

if ($VerifyOnly) {
    & ssh -b $bindAddress -o BatchMode=yes -o ConnectTimeout=8 cac "printf 'SSH_WLAN_OK user='; id -un; printf ' host='; hostname"
    exit $LASTEXITCODE
}

Write-Host 'Connecting to remote server cac...'
Write-Host 'Keep this window open while using the remote page.'
Write-Host 'Local URL: http://127.0.0.1:18765/'
Write-Host

Start-Process powershell.exe -WindowStyle Hidden -ArgumentList @(
    '-NoProfile',
    '-Command',
    "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:18765/'"
)

& ssh -b $bindAddress -N `
    -o ExitOnForwardFailure=yes `
    -o ServerAliveInterval=30 `
    -o ServerAliveCountMax=3 `
    -L '18765:127.0.0.1:18767' cac
$sshExit = $LASTEXITCODE

Write-Host
Write-Host 'SSH tunnel disconnected.'
Read-Host 'Press Enter to close'
exit $sshExit
