[CmdletBinding()]
param(
    [string] $Profile = "p16",
    [int] $Port = 8811,
    [string] $UiUrl = "http://localhost:7860",
    [string] $ComposeFile = "docker-compose.local.yml"
)

$ErrorActionPreference = "Stop"
$ScriptRootPath = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$WorkspaceRoot = Split-Path -Parent (Split-Path -Parent $ScriptRootPath)
$ComposePath = Join-Path $WorkspaceRoot $ComposeFile

Write-Host "Checking Docker MCP profile '$Profile'..."
$profiles = docker mcp profile list
if ($LASTEXITCODE -ne 0) {
    throw "docker mcp profile list failed."
}
if (($profiles -join "`n") -notmatch "(?m)^\s*$([regex]::Escape($Profile))\s+") {
    throw "Docker MCP profile '$Profile' was not found."
}

Write-Host "Checking Docker MCP tools for profile '$Profile'..."
$tools = docker mcp tools ls --gateway-arg "--profile=$Profile"
if ($LASTEXITCODE -ne 0) {
    throw "docker mcp tools ls failed for profile '$Profile'."
}
$toolCount = ([regex]::Matches(($tools -join "`n"), "(?m)^\s+-\s+")).Count
Write-Host "Profile exposes $toolCount tools."

Write-Host "Checking host port $Port..."
$tcp = Test-NetConnection -ComputerName 127.0.0.1 -Port $Port -WarningAction SilentlyContinue
if (-not $tcp.TcpTestSucceeded) {
    throw "Port $Port is not listening on the host. Run scripts/mcp/start-mcp-gateway.ps1 first."
}

Write-Host "Checking UI container can reach host gateway..."
docker compose -f $ComposePath exec -T ui python -c "import socket; s=socket.create_connection(('host.docker.internal', $Port), 5); s.close(); print('container-ok')"
if ($LASTEXITCODE -ne 0) {
    throw "UI container could not connect to host.docker.internal:$Port."
}

Write-Host "Checking UI MCP proxy..."
$response = Invoke-WebRequest -UseBasicParsing -Uri "$UiUrl/api/mcp/tools" -TimeoutSec 20
$json = $response.Content | ConvertFrom-Json
if (-not $json.configured) {
    throw "UI reports MCP is not configured. Set MCP_GATEWAY_URL in .env and restart ui."
}
if ($json.healthy -eq $false) {
    throw "UI reports MCP gateway offline: $($json.error)"
}
$allowed = @($json.tools | Where-Object { $_.allowed }).Count
Write-Host "MCP proxy is healthy. Allowed tools visible to the app: $allowed."
