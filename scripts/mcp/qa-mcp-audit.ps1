[CmdletBinding()]
param(
    [string] $Profile = "p16",
    [int] $Port = 8811,
    [string] $UiUrl = "http://localhost:7860",
    [string] $EnvPath = "",
    [switch] $RestartGateway
)

$ErrorActionPreference = "Stop"
$ScriptRootPath = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$WorkspaceRoot = Split-Path -Parent (Split-Path -Parent $ScriptRootPath)
if (-not $EnvPath) {
    $EnvPath = Join-Path $WorkspaceRoot ".env"
}

function Write-Step {
    param([string] $Message)
    Write-Output "[mcp-qa] $Message"
}

function Assert-True {
    param(
        [bool] $Condition,
        [string] $Message
    )
    if (-not $Condition) {
        throw $Message
    }
}

function Wait-Port {
    param(
        [int] $TargetPort,
        [int] $TimeoutSeconds = 30
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $client = New-Object System.Net.Sockets.TcpClient
        try {
            $async = $client.BeginConnect("127.0.0.1", $TargetPort, $null, $null)
            if ($async.AsyncWaitHandle.WaitOne(500, $false)) {
                $client.EndConnect($async)
                return $true
            }
        } catch {
            # Keep polling until the deadline.
        } finally {
            $client.Close()
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    return $false
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

function Start-McpGatewayBackground {
    $token = $env:MCP_GATEWAY_AUTH_TOKEN
    if (-not $token) {
        $token = Get-DotEnvValue -Path $EnvPath -Name "MCP_GATEWAY_AUTH_TOKEN"
    }
    if (-not $token) {
        throw "MCP_GATEWAY_AUTH_TOKEN is not set in the environment or $EnvPath."
    }
    $env:MCP_GATEWAY_AUTH_TOKEN = $token

    $stdout = Join-Path $WorkspaceRoot "docker-mcp-gateway.out.log"
    $stderr = Join-Path $WorkspaceRoot "docker-mcp-gateway.err.log"
    $dockerArgs = @("mcp", "gateway", "run", "--transport", "sse", "--port", [string] $Port, "--profile", $Profile)
    $process = Start-Process -FilePath "docker" -ArgumentList $dockerArgs -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    Write-Step "started Docker MCP gateway PID $($process.Id); logs: $stdout and $stderr"
}

function Invoke-UiJson {
    param(
        [ValidateSet("GET", "POST")][string] $Method,
        [string] $Path,
        [object] $Body = $null,
        [int] $TimeoutSeconds = 60
    )
    $uri = "$($UiUrl.TrimEnd('/'))/$($Path.TrimStart('/'))"
    if ($Method -eq "GET") {
        return Invoke-RestMethod -Method Get -Uri $uri -TimeoutSec $TimeoutSeconds
    }

    $json = $Body | ConvertTo-Json -Depth 30
    return Invoke-RestMethod -Method Post -Uri $uri -ContentType "application/json" -Body $json -TimeoutSec $TimeoutSeconds
}

function Invoke-McpTool {
    param(
        [string] $Name,
        [hashtable] $Arguments
    )
    Invoke-UiJson -Method POST -Path "api/mcp/call" -Body @{
        name = $Name
        arguments = $Arguments
    }
}

function Invoke-McpCalls {
    param([array] $Calls)
    Invoke-UiJson -Method POST -Path "api/mcp/call" -Body @{ calls = $Calls }
}

function Get-McpText {
    param([object] $Response)
    $chunks = New-Object System.Collections.Generic.List[string]

    function Add-ContentText {
        param([object] $Node)
        if ($null -eq $Node) {
            return
        }
        if ($Node -is [System.Array]) {
            foreach ($item in $Node) {
                Add-ContentText $item
            }
            return
        }
        if ($Node.PSObject.Properties.Name -contains "content") {
            Add-ContentText $Node.content
        }
        if ($Node.PSObject.Properties.Name -contains "text") {
            $chunks.Add([string] $Node.text)
        }
    }

    if ($Response.PSObject.Properties.Name -contains "result") {
        Add-ContentText $Response.result
    }
    if ($Response.PSObject.Properties.Name -contains "results") {
        Add-ContentText $Response.results
    }
    return ($chunks -join "`n")
}

function Restart-McpGateway {
    Write-Step "restarting Docker MCP gateway on port $Port"
    $gateways = Get-CimInstance Win32_Process -Filter "name = 'docker-mcp.exe'" |
        Where-Object { $_.CommandLine -match "gateway run" -and $_.CommandLine -match "--port $Port" }
    foreach ($gateway in $gateways) {
        Stop-Process -Id $gateway.ProcessId -Force
    }
    Start-Sleep -Seconds 2
    Start-McpGatewayBackground
    Assert-True (Wait-Port -TargetPort $Port -TimeoutSeconds 45) "Docker MCP gateway did not start on port $Port."
}

Write-Step "checking Docker MCP profile '$Profile'"
$profiles = docker mcp profile list
Assert-True ($LASTEXITCODE -eq 0) "docker mcp profile list failed."
Assert-True (($profiles -join "`n") -match "(?m)^\s*$([regex]::Escape($Profile))\s+") "Docker MCP profile '$Profile' was not found."

Write-Step "checking Docker MCP tool catalog for profile '$Profile'"
$toolList = docker mcp tools ls --gateway-arg "--profile=$Profile"
Assert-True ($LASTEXITCODE -eq 0) "docker mcp tools ls failed for profile '$Profile'."
$toolText = $toolList -join "`n"
foreach ($toolName in @("create_entities", "add_observations", "open_nodes", "search_nodes", "sequentialthinking")) {
    Assert-True ($toolText -match "(?m)\b$([regex]::Escape($toolName))\b") "Profile '$Profile' does not expose $toolName."
}

Write-Step "checking gateway port $Port"
if (-not (Wait-Port -TargetPort $Port -TimeoutSeconds 5)) {
    Write-Step "gateway offline; starting it in the background"
    Start-McpGatewayBackground
}
Assert-True (Wait-Port -TargetPort $Port -TimeoutSeconds 45) "Docker MCP gateway is not listening on port $Port."

Write-Step "checking UI MCP health"
$health = Invoke-UiJson -Method GET -Path "api/mcp/health" -TimeoutSeconds 30
Assert-True ([bool] $health.configured) "UI reports MCP is not configured."
Assert-True ([bool] $health.healthy) "UI reports MCP gateway is not healthy: $($health.detail)"

Write-Step "checking UI-visible allowlist"
$tools = Invoke-UiJson -Method GET -Path "api/mcp/tools" -TimeoutSeconds 60
Assert-True ([bool] $tools.healthy) "UI MCP tool endpoint is unhealthy."
$allowedNames = @($tools.tools | Where-Object { $_.allowed } | ForEach-Object { $_.name })
foreach ($toolName in @("create_entities", "add_observations", "open_nodes", "search_nodes", "sequentialthinking")) {
    Assert-True ($allowedNames -contains $toolName) "UI allowlist does not expose $toolName."
}

$runId = "qa-run-{0}" -f (Get-Date -Format "yyyyMMddHHmmss")
$entityName = "QA_MCP_MEMORY_AUDIT"
$marker = "MCP QA persistent marker $runId"

Write-Step "writing memory marker $runId"
$existing = Invoke-McpTool -Name "open_nodes" -Arguments @{ names = @($entityName) }
$existingText = Get-McpText $existing
if ($existingText -match [regex]::Escape($entityName)) {
    $write = Invoke-McpCalls -Calls @(
        @{ name = "add_observations"; arguments = @{ observations = @(@{ entityName = $entityName; contents = @($marker) }) } },
        @{ name = "open_nodes"; arguments = @{ names = @($entityName) } },
        @{ name = "search_nodes"; arguments = @{ query = $runId } }
    )
} else {
    $write = Invoke-McpCalls -Calls @(
        @{ name = "create_entities"; arguments = @{ entities = @(@{ name = $entityName; entityType = "qa_audit"; observations = @($marker) }) } },
        @{ name = "open_nodes"; arguments = @{ names = @($entityName) } },
        @{ name = "search_nodes"; arguments = @{ query = $runId } }
    )
}
$writeText = Get-McpText $write
Assert-True ($writeText -match [regex]::Escape($marker)) "Memory marker was not visible in the write-session verification."

Write-Step "checking memory marker in separate MCP sessions"
$open = Invoke-McpTool -Name "open_nodes" -Arguments @{ names = @($entityName) }
Assert-True ((Get-McpText $open) -match [regex]::Escape($marker)) "Memory marker was not visible through open_nodes in a separate session."
$search = Invoke-McpTool -Name "search_nodes" -Arguments @{ query = $runId }
Assert-True ((Get-McpText $search) -match [regex]::Escape($marker)) "Memory marker was not visible through search_nodes in a separate session."

if ($RestartGateway) {
    Restart-McpGateway
    Write-Step "checking memory marker after gateway restart"
    $postRestartHealth = Invoke-UiJson -Method GET -Path "api/mcp/health" -TimeoutSeconds 30
    Assert-True ([bool] $postRestartHealth.healthy) "UI reports MCP unhealthy after gateway restart: $($postRestartHealth.detail)"
    $postRestart = Invoke-McpTool -Name "search_nodes" -Arguments @{ query = $runId }
    Assert-True ((Get-McpText $postRestart) -match [regex]::Escape($marker)) "Memory marker did not survive gateway restart."
}

Write-Step "checking sequentialthinking tool"
$thought = Invoke-McpTool -Name "sequentialthinking" -Arguments @{
    thought = "MCP QA smoke test for the local voice assistant."
    thoughtNumber = 1
    totalThoughts = 1
    nextThoughtNeeded = $false
}
$thoughtText = Get-McpText $thought
Assert-True ($thoughtText.Length -gt 0) "sequentialthinking returned no text content."

Write-Step "PASS: MCP tools, persistent memory, and sequentialthinking are functional through the UI proxy."
