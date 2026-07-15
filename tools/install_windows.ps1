param(
    [switch]$NoLaunch,
    [switch]$NoShortcut,
    [string]$InstallParent
)

$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Windows.Forms

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$DistDir = Join-Path $ProjectRoot "dist\PluralEyesClone"
$ExeName = "PluralEyesClone.exe"
$SourceExe = Join-Path $DistDir $ExeName

function Show-Info {
    param([string]$Message)
    [System.Windows.Forms.MessageBox]::Show(
        $Message,
        "PluralEyes Clone",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Information
    ) | Out-Null
}

function Show-ErrorBox {
    param([string]$Message)
    [System.Windows.Forms.MessageBox]::Show(
        $Message,
        "PluralEyes Clone - erro",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error
    ) | Out-Null
}

function Ask-YesNo {
    param([string]$Message)
    $result = [System.Windows.Forms.MessageBox]::Show(
        $Message,
        "PluralEyes Clone",
        [System.Windows.Forms.MessageBoxButtons]::YesNo,
        [System.Windows.Forms.MessageBoxIcon]::Question
    )
    return $result -eq [System.Windows.Forms.DialogResult]::Yes
}

function Find-PythonCommand {
    $knownPython = Join-Path $env:LOCALAPPDATA "Programs\Python\Python39\python.exe"
    if (Test-Path $knownPython) {
        return @($knownPython)
    }

    $pyLauncher = Get-Command "py" -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        return @("py", "-3")
    }

    $python = Get-Command "python" -ErrorAction SilentlyContinue
    if ($python) {
        return @("python")
    }

    return $null
}

function Invoke-PythonScript {
    param(
        [string[]]$PythonCommand,
        [string]$ScriptPath
    )

    if ($PythonCommand.Count -eq 1) {
        & $PythonCommand[0] $ScriptPath
        return $LASTEXITCODE
    }

    & $PythonCommand[0] $PythonCommand[1] $ScriptPath
    return $LASTEXITCODE
}

function Ensure-Dist {
    if (Test-Path $SourceExe) {
        return
    }

    $shouldBuild = Ask-YesNo "O executavel ainda nao foi gerado neste download. Deseja criar o executavel agora? Isso pode levar alguns minutos."
    if (-not $shouldBuild) {
        throw "Instalacao cancelada: executavel nao encontrado."
    }

    $pythonCommand = Find-PythonCommand
    if (-not $pythonCommand) {
        Show-ErrorBox "Python nao foi encontrado. Instale Python 3.9 ou superior e execute o instalador novamente."
        throw "Python nao encontrado."
    }

    $buildScript = Join-Path $ProjectRoot "tools\build_exe.py"
    if (-not (Test-Path $buildScript)) {
        throw "Script de build nao encontrado: $buildScript"
    }

    Write-Host ""
    Write-Host "Gerando executavel. Aguarde..."
    $exitCode = Invoke-PythonScript -PythonCommand $pythonCommand -ScriptPath $buildScript
    if ($exitCode -ne 0) {
        throw "Build do executavel falhou com exit=$exitCode"
    }

    if (-not (Test-Path $SourceExe)) {
        throw "Build terminou, mas o executavel nao foi encontrado: $SourceExe"
    }
}

function Select-InstallParent {
    $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
    $dialog.Description = "Escolha onde salvar/instalar o PluralEyes Clone"
    $dialog.ShowNewFolderButton = $true

    $desktop = [Environment]::GetFolderPath("Desktop")
    if ($desktop) {
        $dialog.SelectedPath = $desktop
    }

    $result = $dialog.ShowDialog()
    if ($result -ne [System.Windows.Forms.DialogResult]::OK) {
        return $null
    }

    return $dialog.SelectedPath
}

function Install-App {
    param([string]$InstallParent)

    $installDir = Join-Path $InstallParent "PluralEyesClone"
    if (Test-Path $installDir) {
        $overwrite = Ask-YesNo "A pasta ja existe:`n$installDir`n`nDeseja substituir essa instalacao?"
        if (-not $overwrite) {
            throw "Instalacao cancelada pelo usuario."
        }
        Remove-Item -LiteralPath $installDir -Recurse -Force
    }

    New-Item -ItemType Directory -Path $installDir -Force | Out-Null
    Copy-Item -Path (Join-Path $DistDir "*") -Destination $installDir -Recurse -Force

    return $installDir
}

function Create-DesktopShortcut {
    param([string]$InstallDir)

    $target = Join-Path $InstallDir $ExeName
    $desktop = [Environment]::GetFolderPath("Desktop")
    if (-not $desktop) {
        return
    }

    $shortcutPath = Join-Path $desktop "PluralEyes Clone.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $target
    $shortcut.WorkingDirectory = $InstallDir
    $shortcut.IconLocation = $target
    $shortcut.Save()
}

try {
    Ensure-Dist

    if ([string]::IsNullOrWhiteSpace($InstallParent)) {
        $installParent = Select-InstallParent
    }
    if (-not $installParent) {
        Write-Host "Instalacao cancelada."
        exit 0
    }

    $installDir = Install-App -InstallParent $installParent
    if (-not $NoShortcut) {
        Create-DesktopShortcut -InstallDir $installDir
    }

    if ($NoShortcut) {
        Show-Info "PluralEyes Clone instalado em:`n$installDir"
    }
    else {
        Show-Info "PluralEyes Clone instalado em:`n$installDir`n`nUm atalho foi criado na area de trabalho."
    }
    Start-Process explorer.exe $installDir

    if (-not $NoLaunch) {
        Start-Process (Join-Path $installDir $ExeName)
    }

    exit 0
}
catch {
    Show-ErrorBox $_.Exception.Message
    Write-Error $_
    exit 1
}
