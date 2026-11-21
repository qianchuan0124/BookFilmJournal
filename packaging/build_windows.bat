@echo off
rem 本脚本以 UTF-8 保存，中文提示需要 65001 才能正常显示（默认 936 下会乱码）
chcp 65001 >nul
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
rem 依赖已装齐时完全离线：先按本地已装版本校验，缺失或版本不符才联网安装。
rem 不做 pip 自升级——代理不稳时会长时间空转卡住，而它对打包没有实际作用。
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --no-index --quiet -r requirements-build.txt >nul 2>&1
if errorlevel 1 (
  echo 正在安装打包依赖，首次运行需要联网，请耐心等待...
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --timeout 30 --retries 2 -r requirements-build.txt
  if errorlevel 1 exit /b 1
)
if exist "dist\ShuyingJilu" rmdir /s /q "dist\ShuyingJilu"
del /f /q "dist\*.zip" 2>nul

".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean packaging\ShuyingJilu.spec
if errorlevel 1 exit /b 1

if not exist "dist\ShuyingJilu\ShuyingJilu.exe" (
  echo 未生成 Windows 应用：dist\ShuyingJilu\ShuyingJilu.exe
  exit /b 1
)

set ZIP=dist\ShuyingJilu-win.zip
if exist "%ZIP%" del /f /q "%ZIP%"
rem Compress-Archive 在文件被占用时会静默失败，改用 .NET ZipFile 并在出错时返回非零退出码
powershell -NoProfile -Command "$ErrorActionPreference='Stop'; Add-Type -AssemblyName System.IO.Compression.FileSystem; [System.IO.Compression.ZipFile]::CreateFromDirectory('%CD%\dist\ShuyingJilu','%CD%\dist\ShuyingJilu-win.zip',[System.IO.Compression.CompressionLevel]::Optimal,$false)"
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

rem 版本号走临时文件：for /f 里嵌套引号会被 cmd 的错误引号规则吞掉，导致读到空版本
set "APP_VERSION="
".venv\Scripts\python.exe" -c "import sys;sys.path.insert(0,'.');from app.config import APP_VERSION;print(APP_VERSION)" > "%TEMP%\bfj_version.txt" 2>nul
if exist "%TEMP%\bfj_version.txt" set /p APP_VERSION=<"%TEMP%\bfj_version.txt"
del /f /q "%TEMP%\bfj_version.txt" 2>nul
set "VER_ARG="
if defined APP_VERSION set "VER_ARG=/DMyAppVersion=%APP_VERSION%"
if not defined VER_ARG echo 未读取到应用版本号，安装包将使用 ShuyingJilu.iss 里的默认值。

"%ISCC%" %VER_ARG% packaging\ShuyingJilu.iss
if errorlevel 1 exit /b 1

if not exist "dist\ShuyingJilu-win.setup.exe" (
  echo 未生成 Windows 安装包：dist\ShuyingJilu-win.setup.exe
  exit /b 1
)
echo Windows 安装包已生成：dist\ShuyingJilu-win.setup.exe
