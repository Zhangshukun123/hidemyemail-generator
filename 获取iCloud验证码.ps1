[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidatePattern('^[^\s@]+@icloud\.com$')]
    [string]$Email,
    [ValidateRange(1, 600)]
    [int]$TimeoutSeconds = 120,
    [string]$ServiceUrl = "https://icloud-code.8-208-13-52.sslip.io",
    [string]$Since = ""
)

$ErrorActionPreference = "Stop"
$token = [Environment]::GetEnvironmentVariable("HIDEMYEMAIL_REMOTE_TOKEN", "Process")
if (-not $token) {
    $token = [Environment]::GetEnvironmentVariable("HIDEMYEMAIL_REMOTE_TOKEN", "User")
}
if (-not $token -or $token.Length -lt 32) {
    throw "HIDEMYEMAIL_REMOTE_TOKEN is missing or too short."
}

if (-not $Since) {
    $Since = [DateTime]::UtcNow.ToString("o")
}
$endpoint = $ServiceUrl.TrimEnd("/") + "/api/integrations/workbench/openai-code"
$body = @{ email = $Email.Trim().ToLowerInvariant(); since = $Since } |
    ConvertTo-Json -Compress
$deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)

while ([DateTime]::UtcNow -lt $deadline) {
    try {
        $response = Invoke-RestMethod `
            -Uri $endpoint `
            -Method Post `
            -Headers @{ "X-HME-Import-Token" = $token } `
            -ContentType "application/json" `
            -Body $body `
            -TimeoutSec ([Math]::Min(30, $TimeoutSeconds))
        if ($response.ok -and "$($response.code)" -match '^\d{6}$') {
            Write-Output $response.code
            exit 0
        }
    }
    catch {
        $statusCode = 0
        if ($_.Exception.Response) {
            $statusCode = [int]$_.Exception.Response.StatusCode
        }
        if ($statusCode -ne 404) {
            throw
        }
    }
    Start-Sleep -Seconds 5
}

throw "Timed out waiting for an iCloud verification code ($TimeoutSeconds seconds)."
