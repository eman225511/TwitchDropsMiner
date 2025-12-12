@echo off
setlocal enabledelayedexpansion

echo ============================================
echo Twitch Drops Miner - Pre-Release Creator
echo ============================================
echo.

REM Check if gh CLI is installed
where gh >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] GitHub CLI 'gh' is not installed or not in PATH.
    echo Please install it from: https://cli.github.com/
    pause
    exit /b 1
)

REM Check if authenticated with GitHub
gh auth status >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Not authenticated with GitHub CLI.
    echo Please run: gh auth login
    pause
    exit /b 1
)

REM Check if Docker is installed
where docker >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Docker is not installed or not in PATH.
    echo Please install Docker Desktop from: https://www.docker.com/products/docker-desktop/
    pause
    exit /b 1
)

REM Check if Docker is running
docker info >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Docker is not running.
    echo Please start Docker Desktop and try again.
    pause
    exit /b 1
)

echo [1/9] Getting git information...
for /f "tokens=*" %%i in ('git rev-parse --short HEAD') do set SHA_SHORT=%%i
for /f "tokens=*" %%i in ('git rev-parse HEAD') do set SHA_FULL=%%i
for /f "tokens=*" %%i in ('git branch --show-current') do set BRANCH=%%i

echo Git SHA: %SHA_FULL% ^(%SHA_SHORT%^)
echo Branch: %BRANCH%
echo.

REM Ask for confirmation
set /p CONFIRM="Create pre-release with Windows + Linux builds? (y/n): "
if /i not "%CONFIRM%"=="y" (
    echo Cancelled.
    pause
    exit /b 0
)

echo.
echo [2/9] Appending git revision to version.py...
REM Backup original version.py
copy version.py version.py.backup >nul

REM Use venv Python if available, otherwise system Python
if exist ".venv\Scripts\python.exe" (
    set PYTHON_CMD=.venv\Scripts\python.exe
) else (
    set PYTHON_CMD=python
)

REM Read current version and append SHA
for /f "tokens=*" %%i in ('%PYTHON_CMD% -c "from version import __version__; print(__version__)"') do set VERSION=%%i
set NEW_VERSION=%VERSION%.%SHA_SHORT%

REM Update version.py using Python to avoid encoding issues
%PYTHON_CMD% -c "import re; content = open('version.py', 'r', encoding='utf-8').read(); content = re.sub(r'^__version__\s*=\s*\"([^\"]+)\"', r'__version__ = \"\1.%SHA_SHORT%\"', content, flags=re.MULTILINE); open('version.py', 'w', encoding='utf-8').write(content)"

echo Updated version to: %NEW_VERSION%
echo.

echo ============================================
echo         BUILDING WINDOWS VERSION
echo ============================================
echo.

echo [3/9] Checking virtual environment...
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv
)
set PYTHON=.venv\Scripts\python.exe
set PYINSTALLER=.venv\Scripts\pyinstaller.exe

echo [4/9] Installing Windows dependencies...
%PYTHON% -m pip install wheel >nul 2>nul
%PYTHON% -m pip install -r requirements.txt >nul 2>nul
%PYTHON% -m pip install pyinstaller >nul 2>nul
echo Dependencies installed.
echo.

echo [5/9] Building Windows executable with PyInstaller...
%PYINSTALLER% build.spec
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Windows build failed!
    move /y version.py.backup version.py >nul
    pause
    exit /b 1
)
echo Windows build completed successfully.
echo.

echo [6/9] Creating Windows release package...
set FOLDER_NAME=Twitch Drops Miner
if exist "%FOLDER_NAME%" rmdir /s /q "%FOLDER_NAME%"
if exist "Twitch.Drops.Miner.Windows.zip" del "Twitch.Drops.Miner.Windows.zip"

mkdir "%FOLDER_NAME%"
copy dist\*.exe "%FOLDER_NAME%\" >nul
copy manual.txt "%FOLDER_NAME%\" >nul
powershell -Command "Compress-Archive -Path '%FOLDER_NAME%' -DestinationPath 'Twitch.Drops.Miner.Windows.zip'"

echo Windows package created: Twitch.Drops.Miner.Windows.zip
echo.

echo ============================================
echo         BUILDING LINUX VERSIONS
echo ============================================
echo.

echo [7/9] Building Linux PyInstaller (x86_64)...
docker run --rm -v "%CD%:/work" -w /work ubuntu:22.04 bash -c "apt update && apt install -y python3 python3-pip python3-tk libgirepository1.0-dev gir1.2-ayatanaappindicator3-0.1 libayatana-appindicator3-1 xvfb curl build-essential libfreetype6-dev libfontconfig1-dev libxrender-dev && python3 -m pip install --break-system-packages wheel && python3 -m pip install --break-system-packages -r requirements.txt && python3 -m pip install --break-system-packages pyinstaller && mkdir -p /tmp/libXft && cd /tmp/libXft && curl -fL https://xorg.freedesktop.org/releases/individual/lib/libXft-2.3.9.tar.xz -o libXft.tar.xz && tar xvf libXft.tar.xz && cd libXft-* && ./configure --prefix=/tmp/libXft --sysconfdir=/etc --disable-static && make && make install-strip && cd /work && LD_LIBRARY_PATH=/tmp/libXft/lib xvfb-run --auto-servernum pyinstaller build.spec && mkdir -p 'Twitch Drops Miner' && cp manual.txt dist/* 'Twitch Drops Miner/' && apt install -y p7zip-full && 7z a 'Twitch.Drops.Miner.Linux.PyInstaller-x86_64.zip' 'Twitch Drops Miner'"

if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Linux PyInstaller build failed!
    move /y version.py.backup version.py >nul
    pause
    exit /b 1
)
echo Linux PyInstaller build completed successfully.
echo.

echo [8/9] Building Linux AppImage (x86_64)...
docker run --rm -v "%CD%:/work" -w /work --privileged ubuntu:22.04 bash -c "apt update && apt install -y python3 python3-pip libgirepository1.0-dev gir1.2-ayatanaappindicator3-0.1 libayatana-appindicator3-1 git fuse p7zip-full file && python3 -m pip install --break-system-packages git+https://github.com/AppImageCrafters/appimage-builder.git@e995e8edcc227d14524cf39f9824c238f9435a22 && cd /work && export ARCH=x86_64 && export ARCH_APT=amd64 && export APP_VERSION=%NEW_VERSION% && export PYTHON_VERSION=3.10 && appimage-builder --recipe appimage/AppImageBuilder.yml --skip-test && mkdir -p 'Twitch Drops Miner' && cp *.AppImage manual.txt 'Twitch Drops Miner/' 2>/dev/null || true && 7z a 'Twitch.Drops.Miner.Linux.AppImage-x86_64.zip' 'Twitch Drops Miner'"

if %ERRORLEVEL% NEQ 0 (
    echo [WARNING] Linux AppImage build failed, continuing anyway...
) else (
    echo Linux AppImage build completed successfully.
)
echo.

echo ============================================
echo       UPLOADING TO GITHUB RELEASE
echo ============================================
echo.

echo [9/9] Creating GitHub pre-release...

REM Delete existing dev-build release if it exists
gh release view dev-build >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    echo Deleting existing dev-build release...
    gh release delete dev-build --cleanup-tag --yes
)

REM Get current timestamp
for /f "tokens=*" %%i in ('powershell -Command "Get-Date -Format 'yyyy-MM-dd HH:mm:ss K'"') do set DATE_NOW=%%i

REM Collect all build artifacts
set ARTIFACTS=Twitch.Drops.Miner.Windows.zip
if exist "Twitch.Drops.Miner.Linux.PyInstaller-x86_64.zip" set ARTIFACTS=!ARTIFACTS! Twitch.Drops.Miner.Linux.PyInstaller-x86_64.zip
if exist "Twitch.Drops.Miner.Linux.AppImage-x86_64.zip" set ARTIFACTS=!ARTIFACTS! Twitch.Drops.Miner.Linux.AppImage-x86_64.zip

REM Create release notes
set "NOTES=**This is an automatically generated in-development pre-release version of the application, that includes the latest master branch changes.**\n\n**⚠️ This build is not stable and may end up terminating with a fatal error. ⚠️**\n\n**Use at your own risk.**\n\n- Last build date: `%DATE_NOW%`\n- Reference commit: %SHA_FULL%"

REM Create new pre-release
echo Creating new pre-release with artifacts...
gh release create dev-build %ARTIFACTS% --prerelease --title "Development build" --notes "!NOTES!"

if %ERRORLEVEL% EQU 0 (
    echo.
    echo ============================================
    echo SUCCESS! Pre-release created and uploaded.
    echo ============================================
    echo.
    echo Artifacts uploaded:
    for %%f in (%ARTIFACTS%) do echo   - %%f
    echo.
    echo Release URL: https://github.com/eman225511/TwitchDropsMiner/releases/tag/dev-build
) else (
    echo [ERROR] Failed to create release.
)

echo.
echo [Cleanup] Restoring original version.py...
move /y version.py.backup version.py >nul

echo.
echo [Cleanup] Removing build artifacts...
if exist "dist" rmdir /s /q "dist"
if exist "build" rmdir /s /q "build"
if exist "%FOLDER_NAME%" rmdir /s /q "%FOLDER_NAME%"

echo.
echo Done!
pause
