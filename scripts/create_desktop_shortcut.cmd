@echo off
setlocal
set "VARSCOPE_APPDIR=%~dp0."
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$appDirPath=(Resolve-Path -LiteralPath $env:VARSCOPE_APPDIR).Path;" ^
  "$exePath=Join-Path $appDirPath 'VarScope.exe';" ^
  "if(-not (Test-Path -LiteralPath $exePath)){throw ('VarScope.exe not found in: '+$appDirPath)}" ^
  "$iconPath=Join-Path $appDirPath 'VarScope.ico';" ^
  "$iconLocation=if(Test-Path -LiteralPath $iconPath){$iconPath}else{$exePath};" ^
  "$desktop=[Environment]::GetFolderPath('DesktopDirectory');" ^
  "$linkPath=Join-Path $desktop 'VarScope.lnk';" ^
  "$shell=New-Object -ComObject WScript.Shell;" ^
  "$link=$shell.CreateShortcut($linkPath);" ^
  "$link.TargetPath=$exePath;" ^
  "$link.WorkingDirectory=$appDirPath;" ^
  "$link.IconLocation=$iconLocation;" ^
  "$link.Description='VarScope';" ^
  "$link.Save();" ^
  "Write-Host ('Desktop shortcut created: '+$linkPath)"
if errorlevel 1 (
    echo.
    echo Failed to create VarScope desktop shortcut.
    echo Please make sure this file is inside the VarScope folder and try again.
    pause
    exit /b 1
)
echo Desktop shortcut created successfully.