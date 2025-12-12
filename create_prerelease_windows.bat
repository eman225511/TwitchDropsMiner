@echo off
setlocal enabledelayedexpansion

echo ============================================
echo Twitch Drops Miner - Windows Pre-Release
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

echo [1/6] Getting git information...
for /f "tokens=*" %%i in ('git rev-parse --short HEAD') do set SHA_SHORT=%%i
for /f "tokens=*" %%i in ('git rev-parse HEAD') do set SHA_FULL=%%i
for /f "tokens=*" %%i in ('git branch --show-current') do set BRANCH=%%i

echo Git SHA: %SHA_FULL% ^(%SHA_SHORT%^)
echo Branch: %BRANCH%
echo.

REM Ask for confirmation
set /p CONFIRM="Create Windows pre-release from current commit? (y/n): "
if /i not "%CONFIRM%"=="y" (
    echo Cancelled.
    pause
    exit /b 0
)

echo.
echo [2/6] Appending git revision to version.py...
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

echo [3/6] Checking virtual environment...
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv
)
set PYTHON=.venv\Scripts\python.exe
set PYINSTALLER=.venv\Scripts\pyinstaller.exe

echo [4/6] Installing dependencies...
%PYTHON% -m pip install wheel >nul 2>nul
%PYTHON% -m pip install -r requirements.txt >nul 2>nul
%PYTHON% -m pip install pyinstaller >nul 2>nul
echo Dependencies installed.
echo.

echo [5/6] Building Windows executable with PyInstaller...
%PYINSTALLER% build.spec
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Build failed!
    move /y version.py.backup version.py >nul
    pause
    exit /b 1
)
echo Build completed successfully.
echo.

echo [6/6] Creating release package and uploading...
set FOLDER_NAME=Twitch Drops Miner
if exist "%FOLDER_NAME%" rmdir /s /q "%FOLDER_NAME%"
if exist "Twitch.Drops.Miner.Windows.zip" del "Twitch.Drops.Miner.Windows.zip"

mkdir "%FOLDER_NAME%"
copy dist\*.exe "%FOLDER_NAME%\" >nul
copy manual.txt "%FOLDER_NAME%\" >nul
powershell -Command "Compress-Archive -Path '%FOLDER_NAME%' -DestinationPath 'Twitch.Drops.Miner.Windows.zip'"

echo Package created: Twitch.Drops.Miner.Windows.zip
echo.

REM Delete existing dev-build release if it exists
gh release view dev-build >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    echo Deleting existing dev-build release...
    gh release delete dev-build --cleanup-tag --yes
)

REM Get current timestamp
for /f "tokens=*" %%i in ('powershell -Command "Get-Date -Format 'yyyy-MM-dd HH:mm:ss K'"') do set DATE_NOW=%%i

REM Create release notes
set "NOTES=**This is an automatically generated in-development pre-release version of the application, that includes the latest master branch changes.**\n\n**⚠️ This build is not stable and may end up terminating with a fatal error. ⚠️**\n\n**Use at your own risk.**\n\n- Last build date: `%DATE_NOW%`\n- Reference commit: %SHA_FULL%"

REM Create new pre-release
echo Creating GitHub pre-release...
gh release create dev-build "Twitch.Drops.Miner.Windows.zip" --prerelease --title "Development build" --notes "!NOTES!"

if %ERRORLEVEL% EQU 0 (
    echo.
    echo ============================================
    echo SUCCESS! Pre-release created and uploaded.
    echo ============================================
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
