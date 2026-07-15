Option Explicit

Dim shell, fso, root, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)

command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command " & Chr(34) & _
    "Set-Location -LiteralPath '" & Replace(root, "'", "''") & "'; " & _
    "$pythonw='.\.venv\Scripts\pythonw.exe'; " & _
    "$python='.\.venv\Scripts\python.exe'; " & _
    "if (Test-Path $pythonw) { Start-Process -FilePath $pythonw -ArgumentList 'tkinter_app.py' -WorkingDirectory (Get-Location) } " & _
    "elseif (Test-Path $python) { Start-Process -WindowStyle Hidden -FilePath $python -ArgumentList 'tkinter_app.py' -WorkingDirectory (Get-Location) } " & _
    "elseif (Get-Command py -ErrorAction SilentlyContinue) { Start-Process -WindowStyle Hidden -FilePath 'py' -ArgumentList '-3','tools\bootstrap.py' -WorkingDirectory (Get-Location) } " & _
    "elseif (Get-Command python -ErrorAction SilentlyContinue) { Start-Process -WindowStyle Hidden -FilePath 'python' -ArgumentList 'tools\bootstrap.py' -WorkingDirectory (Get-Location) } " & _
    "else { $installer='.\Instalar_Python39_E_Dependencias.bat'; if (Test-Path $installer) { Start-Process -FilePath $installer -WorkingDirectory (Get-Location) } else { Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.MessageBox]::Show('Python nao encontrado. Rode Instalar_Python39_E_Dependencias.bat.','PluralEyes Clone',[System.Windows.Forms.MessageBoxButtons]::OK,[System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null } }" & _
    Chr(34)

shell.Run command, 0, False
