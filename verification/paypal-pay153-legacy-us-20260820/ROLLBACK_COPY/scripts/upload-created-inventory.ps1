[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InputFile,
    [string]$ServiceUrl = $env:HIDEMYEMAIL_INVENTORY_URL,
    [string]$Username = $env:HIDEMYEMAIL_INVENTORY_USERNAME,
    [string]$Password = $env:HIDEMYEMAIL_INVENTORY_PASSWORD
)

$ErrorActionPreference = 'Stop'

function Invoke-DirectJson {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][ValidateSet('GET', 'POST')][string]$Method,
        [string]$Body,
        [string]$AccessToken
    )

    $curlArgs = @('--noproxy', '*', '--silent', '--show-error', '--request', $Method,
        '--header', 'Accept: application/json')
    if ($AccessToken) {
        $curlArgs += @('--header', "Authorization: Bearer $AccessToken")
    }
    $bodyFile = $null
    try {
        if ($PSBoundParameters.ContainsKey('Body')) {
            $bodyFile = [System.IO.Path]::GetTempFileName()
            [System.IO.File]::WriteAllText($bodyFile, $Body, [System.Text.UTF8Encoding]::new($false))
            $curlArgs += @('--header', 'Content-Type: application/json', '--data-binary', "@$bodyFile")
        }
        $curlArgs += $Uri
        $raw = & curl.exe @curlArgs
        if ($LASTEXITCODE -ne 0) {
            throw "HTTP request failed with curl exit $LASTEXITCODE."
        }
        return $raw | ConvertFrom-Json
    }
    finally {
        if ($bodyFile) {
            Remove-Item -LiteralPath $bodyFile -Force -ErrorAction SilentlyContinue
        }
    }
}

if (-not $ServiceUrl -or -not $Username -or -not $Password) {
    throw 'Inventory service URL, username, or password is missing.'
}

$emails = Get-Content -LiteralPath $InputFile |
    ForEach-Object { $_.Trim().ToLowerInvariant() } |
    Where-Object { $_ -match '^[^\s@]+@icloud\.com$' } |
    Sort-Object -Unique

if (-not $emails) {
    throw 'No valid iCloud addresses were found in the input file.'
}

$loginBody = @{ username = $Username; password = $Password } | ConvertTo-Json -Compress
$login = Invoke-DirectJson -Uri "$($ServiceUrl.TrimEnd('/'))/api/integrations/registration-inventory/login" `
    -Method POST -Body $loginBody
if (-not $login.ok -or -not $login.accessToken) {
    throw 'Inventory service login failed.'
}

$timestamp = (Get-Date).ToUniversalTime().ToString('o')
$records = @($emails | ForEach-Object {
    @{
        email = $_
        address = @{
            email = $_
            label = 'Codex 2026-08-12'
            state = 'unused'
            source = 'generated'
            note = 'Imported from Apple Hide My Email Creator'
            is_active = 1
            batch_id = "apple-hme:created-5-20260812:$($_)"
            created_at = $timestamp
            updated_at = $timestamp
        }
        account = $null
    }
})

$payload = @{ schemaVersion = 1; records = $records } | ConvertTo-Json -Depth 8 -Compress
$sync = Invoke-DirectJson -Uri "$($ServiceUrl.TrimEnd('/'))/api/integrations/registration-inventory/sync" `
    -Method POST -AccessToken $login.accessToken -Body $payload
if (-not $sync.ok) {
    throw "Inventory sync failed: $($sync.error)"
}
$status = Invoke-DirectJson -Uri "$($ServiceUrl.TrimEnd('/'))/api/integrations/registration-inventory/status" `
    -Method GET -AccessToken $login.accessToken

Write-Output ("SYNC_OK input={0} imported={1} available={2} activeLeases={3}" -f `
    $records.Count, $sync.addresses, $status.available, $status.activeLeases)
