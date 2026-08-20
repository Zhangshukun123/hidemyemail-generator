[CmdletBinding()]
param(
    [string]$ServerUrl = "https://protocol-register.8-208-13-52.sslip.io",
    [string]$Token = $env:PROTOCOL_SERVER_API_TOKEN,
    [ValidateSet("all", "offer", "no_offer", "pending")]
    [string]$Pool = "all",
    [ValidateRange(1, 500)]
    [int]$Limit = 100,
    [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($Token)) {
    throw "请通过 -Token 或 PROTOCOL_SERVER_API_TOKEN 提供服务器 API 令牌"
}

$query = [System.Web.HttpUtility]::ParseQueryString("")
$query["pool"] = $Pool
$query["limit"] = [string]$Limit
$query["credentials"] = "1"
$uri = $ServerUrl.TrimEnd("/") + "/api/accounts?" + $query.ToString()
$headers = @{ Authorization = "Bearer $Token" }
$result = Invoke-RestMethod -Method Get -Uri $uri -Headers $headers
if (-not $result.ok) {
    throw ($result.error | Out-String)
}

$json = $result | ConvertTo-Json -Depth 100
if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $json
    return
}

$absolutePath = [System.IO.Path]::GetFullPath($OutputPath)
$parent = Split-Path -Parent $absolutePath
if ($parent) {
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
}
[System.IO.File]::WriteAllText($absolutePath, $json, [System.Text.UTF8Encoding]::new($false))
Write-Output $absolutePath
