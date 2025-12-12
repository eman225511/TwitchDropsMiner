@echo off

REM Get the directory path of the script
set "dirpath=%~dp0"
if "%dirpath:~-1%" == "\" set "dirpath=%dirpath:~0,-1%"

REM Check if the virtual environment exists
if not exist "%dirpath%\.venv" (
    echo:
    echo No virtual environment found! Run setup_env.bat to set it up first.
    echo:
    if not "%~1"=="--nopause" pause
    exit /b 1
)

REM Check if PyInstaller and pywin32 is installed in the virtual environment
if not exist "%dirpath%\.venv\Scripts\pyinstaller.exe" (
    echo Installing PyInstaller...
    "%dirpath%\.venv\Scripts\pip" install pyinstaller
    if errorlevel 1 (
        echo:
        echo Failed to install PyInstaller.
        echo:
        if not "%~1"=="--nopause" pause
        exit /b 1
    )
    "%dirpath%\.venv\Scripts\python" "%dirpath%\.venv\Scripts\pywin32_postinstall.py" -install -silent
    if errorlevel 1 (
        echo:
        echo Failed to run pywin32_postinstall.py.
        echo:
        if not "%~1"=="--nopause" pause
        exit /b 1
    )
)

REM Run PyInstaller with the specified build spec file
echo Building...
"%dirpath%\.venv\Scripts\pyinstaller" "%dirpath%\build.spec"
if errorlevel 1 (
    echo:
    echo PyInstaller build failed.
    echo:
    if not "%~1"=="--nopause" pause
    exit /b 1
)

echo:
echo Build completed successfully.
echo:
if not "%~1"=="--nopause" pause
