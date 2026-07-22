param(
    [switch]$NoOpenApp
)

$ErrorActionPreference = "Stop"

$PythonVersion = "3.9.13"
$PythonInstallerUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$Requirements = Join-Path $ProjectRoot "requirements.txt"
$VenvDir = Join-Path $ProjectRoot ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$VenvPythonw = Join-Path $VenvDir "Scripts\pythonw.exe"
$AppScript = Join-Path $ProjectRoot "WaveSync.py"
$SupportRelayConfig = Join-Path $ProjectRoot "support_relay.json"
$SupportConfigAppData = if ($env:APPDATA) { Join-Path (Join-Path $env:APPDATA "WaveSync") "support_config.json" } else { $null }

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "================================================================"
    Write-Host $Message
    Write-Host "================================================================"
}

function Test-Python39 {
    param([string]$PythonPath)

    if ([string]::IsNullOrWhiteSpace($PythonPath) -or -not (Test-Path $PythonPath)) {
        return $false
    }

    try {
        $version = & $PythonPath -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        return $version -eq "3.9"
    }
    catch {
        return $false
    }
}

function Find-Python39 {
    $candidates = @()

    if ($env:LOCALAPPDATA) {
        $candidates += Join-Path $env:LOCALAPPDATA "Programs\Python\Python39\python.exe"
    }
    if ($env:ProgramFiles) {
        $candidates += Join-Path $env:ProgramFiles "Python39\python.exe"
    }
    if (${env:ProgramFiles(x86)}) {
        $candidates += Join-Path ${env:ProgramFiles(x86)} "Python39\python.exe"
    }

    foreach ($candidate in $candidates) {
        if (Test-Python39 $candidate) {
            return (Resolve-Path $candidate).Path
        }
    }

    $py = Get-Command "py" -ErrorAction SilentlyContinue
    if ($py) {
        try {
            $fromLauncher = & py -3.9 -c "import sys; print(sys.executable)" 2>$null
            if (Test-Python39 $fromLauncher) {
                return $fromLauncher
            }
        }
        catch {
        }
    }

    $python = Get-Command "python" -ErrorAction SilentlyContinue
    if ($python -and (Test-Python39 $python.Source)) {
        return $python.Source
    }

    return $null
}

function Install-Python39-WithWinget {
    $winget = Get-Command "winget" -ErrorAction SilentlyContinue
    if (-not $winget) {
        return $false
    }

    Write-Step "Tentando instalar Python 3.9 via winget"
    $args = @(
        "install",
        "--exact",
        "--id",
        "Python.Python.3.9",
        "--scope",
        "user",
        "--accept-package-agreements",
        "--accept-source-agreements"
    )

    & winget @args
    return $LASTEXITCODE -eq 0
}

function Install-Python39-FromPythonOrg {
    Write-Step "Baixando Python $PythonVersion do site oficial"
    $installer = Join-Path $env:TEMP "python-$PythonVersion-amd64.exe"

    Invoke-WebRequest -Uri $PythonInstallerUrl -OutFile $installer

    Write-Step "Instalando Python $PythonVersion"
    $arguments = @(
        "/quiet",
        "InstallAllUsers=0",
        "PrependPath=1",
        "Include_launcher=1",
        "Include_pip=1",
        "Include_tcltk=1",
        "Include_test=0",
        "SimpleInstall=1"
    )

    $process = Start-Process -FilePath $installer -ArgumentList $arguments -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "Instalador do Python terminou com exit=$($process.ExitCode)"
    }
}

function Ensure-Python39 {
    $pythonPath = Find-Python39
    if ($pythonPath) {
        Write-Host "[OK] Python 3.9 encontrado: $pythonPath"
        return $pythonPath
    }

    $installedByWinget = Install-Python39-WithWinget
    if ($installedByWinget) {
        $pythonPath = Find-Python39
        if ($pythonPath) {
            Write-Host "[OK] Python 3.9 instalado: $pythonPath"
            return $pythonPath
        }
    }

    Install-Python39-FromPythonOrg

    $pythonPath = Find-Python39
    if (-not $pythonPath) {
        throw "Python 3.9 foi instalado, mas nao foi encontrado automaticamente. Feche e abra o terminal e tente novamente."
    }

    Write-Host "[OK] Python 3.9 instalado: $pythonPath"
    return $pythonPath
}

function Ensure-RequirementsFile {
    if (-not (Test-Path $Requirements)) {
        throw "requirements.txt nao encontrado: $Requirements"
    }
}

function Ensure-Venv {
    param([string]$PythonPath)

    Write-Step "Criando ambiente local .venv"
    if (-not (Test-Path $VenvPython)) {
        & $PythonPath -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) {
            throw "Falha ao criar .venv"
        }
    }
    else {
        Write-Host "[OK] .venv ja existe"
    }
}

function Install-Dependencies {
    Write-Step "Instalando dependencias do projeto"

    & $VenvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        throw "Falha ao atualizar pip"
    }

    & $VenvPython -m pip install -r $Requirements
    if ($LASTEXITCODE -ne 0) {
        throw "Falha ao instalar requirements.txt"
    }
}

function Verify-Dependencies {
    Write-Step "Verificando dependencias"
    & $VenvPython -c "import numpy, imageio_ffmpeg; print('Dependencias OK')"
    if ($LASTEXITCODE -ne 0) {
        throw "Falha ao verificar numpy/imageio_ffmpeg"
    }
}

function Install-SupportConfig {
    Write-Step "Verificando envio de diagnostico"

    if (Test-Path $SupportRelayConfig) {
        Write-Host "[OK] Suporte automatico por relay configurado: $SupportRelayConfig"
        return
    }

    if ($SupportConfigAppData -and (Test-Path $SupportConfigAppData)) {
        Write-Host "[OK] Configuracao privada de suporte encontrada em: $SupportConfigAppData"
        return
    }

    Write-Host "[WARN] Suporte automatico nao configurado nesta copia."
    Write-Host "[WARN] O botao de diagnostico vai gerar ZIP local, mas nao enviara automaticamente."
}

function Open-App {
    if ($NoOpenApp) {
        return
    }

    if (-not (Test-Path $AppScript)) {
        Write-Host "[WARN] WaveSync.py nao encontrado. Instalacao concluida, mas a interface nao foi aberta."
        return
    }

    Write-Step "Abrindo interface"
    if (Test-Path $VenvPythonw) {
        Start-Process -FilePath $VenvPythonw -ArgumentList "`"$AppScript`"" -WorkingDirectory $ProjectRoot
    }
    else {
        Start-Process -FilePath $VenvPython -ArgumentList "`"$AppScript`"" -WorkingDirectory $ProjectRoot
    }
}

try {
    Write-Step "Preparando WaveSync"
    Write-Host "Pasta do projeto: $ProjectRoot"

    Ensure-RequirementsFile
    $pythonPath = Ensure-Python39
    Ensure-Venv -PythonPath $pythonPath
    Install-Dependencies
    Verify-Dependencies
    Install-SupportConfig

    Write-Step "Instalacao concluida"
    Write-Host "[OK] Python 3.9 e dependencias estao prontos."
    Write-Host "[OK] Para abrir depois, rode python WaveSync.py na pasta do projeto."

    Open-App
    exit 0
}
catch {
    Write-Host ""
    Write-Host "[ERRO] $($_.Exception.Message)" -ForegroundColor Red
    Write-Host ""
    Write-Host "Se precisar, instale Python 3.9 manualmente em:"
    Write-Host "https://www.python.org/downloads/release/python-$PythonVersion/"
    exit 1
}
