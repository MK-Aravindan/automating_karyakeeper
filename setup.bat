@echo off
setlocal enabledelayedexpansion

:: Change working directory to the directory of the batch file
cd /d "%~dp0"

echo ===================================================
echo KaryaKeeper Automation - Setup
echo ===================================================
echo.

:: 1. Check Python
echo [1/4] Checking Python installation...
python --version >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: Python is not installed or not added to the system PATH.
    echo Please install Python from the Company Portal and ensure you check the box 
    echo "Add Python to PATH" during installation.
    echo.
    pause
    exit /b
)
echo Python is successfully installed.
echo.

:: Note: Python Playwright bundles its own Node.js executable, 
:: so a separate system-wide Node.js installation is not required.

:: 2. Install Python Requirements
echo [2/4] Installing Python dependencies (this may take a minute)...
python -m pip install -q -r app\requirements.txt
IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: Failed to install Python dependencies.
    pause
    exit /b
)
echo Dependencies installed successfully.
echo.

:: 3. Install Playwright Chromium
echo [3/4] Installing Playwright (Chromium)...
set PLAYWRIGHT_BROWSERS_PATH=0
python -m playwright install chromium
IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: Failed to install Playwright browsers.
    pause
    exit /b
)
echo Playwright installed successfully.
echo.

:: 4. Configure .env File
echo [4/4] Checking configuration file...
IF NOT EXIST ".env" (
    echo Creating .env file template...
    (
        echo GREYTHR_DOMAIN=7dxperts
        echo GREYTHR_USERNAME=
        echo GREYTHR_PASSWORD=
        echo KARYAKEEPER_URL=https://app.karyakeeper.com/
        echo KARYAKEEPER_USERNAME=
        echo KARYAKEEPER_PASSWORD=
    ) > .env
    echo.
    echo A new .env file has been created.
    echo Opening .env file in Notepad for you to configure...
    start notepad .env
) ELSE (
    echo .env file already exists.
)

echo.
echo ===================================================
echo Setup is complete! 
echo You can now run the application using 'run.bat'.
echo ===================================================
pause
