@echo off
setlocal
cd /d "%~dp0"
set "PROJECT_DIR=%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -Command "$desktop = [Environment]::GetFolderPath('Desktop'); $path = Join-Path $desktop 'NoteBridge.lnk'; if (-not (Test-Path $path)) { $shell = New-Object -ComObject WScript.Shell; $shortcut = $shell.CreateShortcut($path); $shortcut.TargetPath = Join-Path $env:WINDIR 'System32\cmd.exe'; $shortcut.Arguments = '/c ""' + (Join-Path $env:PROJECT_DIR 'start_desktop.cmd') + '""'; $shortcut.WorkingDirectory = $env:PROJECT_DIR; $shortcut.Description = 'Start NoteBridge desktop application'; $shortcut.IconLocation = Join-Path $env:WINDIR 'System32\shell32.dll,220'; $shortcut.Save() }"

uv run desktop.py
if errorlevel 1 pause