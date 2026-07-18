param(
    [switch]$Remove,
    [switch]$NoStart,
    [switch]$DryRun,
    [string]$HostName = "0.0.0.0",
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$TaskName = "WaveSync Support Relay"
$FirewallRuleName = "WaveSync Support Relay"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$RelayScript = Join-Path $ProjectRoot "tools\support_relay.py"
$SupportConfigProject = Join-Path $ProjectRoot "support_config.json"
$SupportConfigAppData = $null
$SupportConfigLocalAppData = $null
if ($env:APPDATA) {
    $SupportConfigAppData = Join-Path (Join-Path $env:APPDATA "WaveSync") "support_config.json"
}
if ($env:LOCALAPPDATA) {
    $SupportConfigLocalAppData = Join-Path (Join-Path $env:LOCALAPPDATA "WaveSync") "support_config.json"
}
$PythonCandidates = @(
    (Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"),
    (Join-Path $ProjectRoot ".venv\Scripts\python.exe")
)
$LogPath = Join-Path $ProjectRoot "temp\support_relay.log"

function Write-Step($Message) {
    Write-Host "[WaveSync] $Message"
}

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Find-PythonRuntime {
    foreach ($candidate in $PythonCandidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }

    $pythonwCommand = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($pythonwCommand) {
        return $pythonwCommand.Source
    }

    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        return $pythonCommand.Source
    }

    return $null
}

function Find-SupportConfig {
    $paths = @($SupportConfigProject, $SupportConfigAppData, $SupportConfigLocalAppData) | Where-Object { $_ }
    foreach ($path in $paths) {
        if (Test-Path -LiteralPath $path) {
            return $path
        }
    }
    return $null
}

if ($Remove) {
    Write-Step "Removendo tarefa de inicializacao: $TaskName"
    if ($DryRun) {
        Write-Step "DRY RUN: nenhuma alteracao feita."
        exit 0
    }

    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Step "Tarefa removida."
    } else {
        Write-Step "Tarefa nao encontrada. Nada para remover."
    }
    exit 0
}

if (-not (Test-Path -LiteralPath $RelayScript)) {
    throw "Relay nao encontrado: $RelayScript"
}

$PythonRuntime = Find-PythonRuntime
if (-not $PythonRuntime) {
    throw "Python nao encontrado. Rode Instalar_Python39_E_Dependencias.bat antes."
}

$SupportConfigPath = Find-SupportConfig
if (-not $SupportConfigPath) {
    throw "support_config.json nao encontrado. Crie a config privada nesta maquina de suporte antes de instalar o relay."
}

Write-Step "Projeto: $ProjectRoot"
Write-Step "Python: $PythonRuntime"
Write-Step "Relay: $RelayScript"
Write-Step "Config privada encontrada: $SupportConfigPath"
Write-Step "Log: $LogPath"
Write-Step "Endereco local: http://127.0.0.1:$Port/health"
Write-Step "Endereco na rede: http://$env:COMPUTERNAME`:$Port/health"

if ($DryRun) {
    Write-Step "DRY RUN: validacao concluida. Nenhuma alteracao feita."
    exit 0
}

$arguments = "`"$RelayScript`" --host $HostName --port $Port"
try {
    $action = New-ScheduledTaskAction -Execute $PythonRuntime -Argument $arguments -WorkingDirectory $ProjectRoot
} catch {
    $action = New-ScheduledTaskAction -Execute $PythonRuntime -Argument $arguments
}
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
$description = "WaveSync support relay. Recebe diagnosticos na rede local e encaminha ao Telegram."

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description $description -Force | Out-Null
Write-Step "Tarefa criada/atualizada para iniciar com o Windows."

if (Test-Admin) {
    $existingRule = Get-NetFirewallRule -DisplayName $FirewallRuleName -ErrorAction SilentlyContinue
    if (-not $existingRule) {
        New-NetFirewallRule -DisplayName $FirewallRuleName -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -Profile Private | Out-Null
        Write-Step "Regra de firewall criada para a porta $Port em rede privada."
    } else {
        Write-Step "Regra de firewall ja existe."
    }
} else {
    Write-Step "Aviso: execute este instalador como administrador para criar a regra de firewall automaticamente."
}

if (-not $NoStart) {
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 2
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 4 | Out-Null
        Write-Step "Relay iniciado e respondendo em segundo plano."
    } catch {
        Write-Step "Tarefa iniciada, mas o teste local ainda nao respondeu. Veja o log: $LogPath"
    }
}

Write-Step "Pronto. O relay vai abrir oculto a cada login do Windows."
