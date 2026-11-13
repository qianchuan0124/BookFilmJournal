@echo off
setlocal
cd /d "%~dp0\.."

rem 用法：build_windows.bat [--portable-only]
rem   --portable-only  只生成便携目录与 zip，不生成 Inno Setup 安装包
set "PORTABLE_ONLY="
if /i "%~1"=="--portable-only" set "PORTABLE_ONLY=1"

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
rem Compress-Archive 在文件被占用时会静默失败，改用 .NET ZipFile 并在出错时返回非零退出码
powershell -NoProfile -Command "$ErrorActionPreference='Stop'; Add-Type -AssemblyName System.IO.Compression.FileSystem; [System.IO.Compression.ZipFile]::CreateFromDirectory('%CD%\dist\ShuyingJilu','%CD%\dist\ShuyingJilu-windows-x64.zip',[System.IO.Compression.CompressionLevel]::Optimal,$false)"
if errorlevel 1 exit /b 1
if not exist "%ZIP%" (
  echo 未生成 Windows 便携包：%ZIP%
  exit /b 1
)
echo Windows 便携包已生成：%ZIP%

if defined PORTABLE_ONLY (
  echo 已跳过安装包生成（--portable-only）
  exit /b 0
)

rem 定位 Inno Setup 6 的编译器，优先使用 PATH 中的 iscc
set "ISCC="
for %%I in (iscc.exe) do if not defined ISCC set "ISCC=%%~$PATH:I"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC (
  echo 未找到 Inno Setup 6 的 ISCC.exe，无法生成安装包。
  echo 请安装 https://jrsoftware.org/isdl.php 后重试，或用 --portable-only 只打便携包。
  exit /b 1
)

for /f "usebackq delims=" %%v in (`".venv\Scripts\python.exe" -c "import sys;sys.path.insert(0,'.');from app.config import APP_VERSION;print(APP_VERSION)"`) do set "APP_VERSION=%%v"

"%ISCC%" /DMyAppVersion=%APP_VERSION% packaging\ShuyingJilu.iss
if errorlevel 1 exit /b 1

if not exist "dist\ShuyingJilu-windows-x64-setup.exe" (
  echo 未生成 Windows 安装包：dist\ShuyingJilu-windows-x64-setup.exe
  exit /b 1
)
echo Windows 安装包已生成：dist\ShuyingJilu-windows-x64-setup.exe
