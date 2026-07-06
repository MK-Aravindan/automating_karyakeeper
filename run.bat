@echo off
setlocal enabledelayedexpansion

:: Change working directory to the directory of the batch file
cd /d "%~dp0"

echo ===================================================
echo KaryaKeeper Automation - Runner
echo ===================================================
echo.

IF NOT EXIST ".env" (
    echo ERROR: .env file is missing!
    echo Please run setup.bat first to configure the application.
    pause
    exit /b
)

echo Enter the date to log (YYYY-MM-DD) or press Enter to use today's date:
set /p target_date=

echo.
echo Starting automation...
echo.

if "%target_date%"=="" (
    python app\automate_karyakeeper.py
) else (
    python app\automate_karyakeeper.py --date "%target_date%"
)

echo.
pause
