[CmdletBinding()]
param(
    [string] $Profile = "p16",
    [int] $Port = 8811,
    [string] $EnvPath = "",
    [switch] $Background
)

$ErrorActionPreference = "Stop"
$ScriptRootPath = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$WorkspaceRoot = Split-Path -Parent (Split-Path -Parent $ScriptRootPath)
if (-not $EnvPath) {
    $EnvPath = Join-Path $WorkspaceRoot ".env"
}

function Get-DotEnvValue {
    param(
        [Parameter(Mandatory = $true)][string] $Path,
        [Parameter(Mandatory = $true)][string] $Name
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }

    $pattern = "^\s*{0}\s*=(.*)$" -f [regex]::Escape($Name)
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match $pattern) {
            $value = $Matches[1].Trim()
            if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            return $value
        }
    }
    return $null
}

$token = $env:MCP_GATEWAY_AUTH_TOKEN
if (-not $token) {
    $token = Get-DotEnvValue -Path $EnvPath -Name "MCP_GATEWAY_AUTH_TOKEN"
}

if (-not $token) {
    throw "MCP_GATEWAY_AUTH_TOKEN is not set. Add it to .env or set it in this shell before starting the gateway."
}

$env:MCP_GATEWAY_AUTH_TOKEN = $token

$existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Docker MCP gateway already appears to be listening on port $Port."
    exit 0
}

$dockerArgs = @("mcp", "gateway", "run", "--transport", "sse", "--port", [string] $Port, "--profile", $Profile)

if ($Background) {
    $stdout = Join-Path $WorkspaceRoot "docker-mcp-gateway.out.log"
    $stderr = Join-Path $WorkspaceRoot "docker-mcp-gateway.err.log"
    $process = Start-Process -FilePath "docker" -ArgumentList $dockerArgs -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    Start-Sleep -Seconds 2
    Write-Host "Started Docker MCP gateway in background on port $Port with profile $Profile. PID: $($process.Id)"
    Write-Host "Logs: $stdout and $stderr"
    exit 0
}

Write-Host "Starting Docker MCP gateway on port $Port with profile $Profile. Leave this terminal open."
& docker @dockerArgs
