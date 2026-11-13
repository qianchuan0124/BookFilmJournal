@echo off
setlocal
cd /d "%~dp0\.."

if not exist ".venv\Scripts\python.exe" (
  where py >nul 2>nul
  if errorlevel 1 (
    python -m venv .venv
  ) else (
    py -3 -m venv .venv
  )
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pip install -r requirements-build.txt
if errorlevel 1 exit /b 1
if exist "dist\ShuyingJilu" rmdir /s /q "dist\ShuyingJilu"
del /f /q "dist\*.zip" 2>nul

".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean packaging\ShuyingJilu.spec
if errorlevel 1 exit /b 1

if not exist "dist\ShuyingJilu\ShuyingJilu.exe" (
  echo 未生成 Windows 应用：dist\ShuyingJilu\ShuyingJilu.exe
  exit /b 1
)

set ZIP=dist\ShuyingJilu-windows-x64.zip
if exist "%ZIP%" del /f /q "%ZIP%"
powershell -NoProfile -Command "Compress-Archive -Path 'dist\ShuyingJilu\*' -DestinationPath '%ZIP%' -Force"
if errorlevel 1 exit /b 1
echo Windows 包已生成：%ZIP%
