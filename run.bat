@echo off
setlocal

:: Change working directory to the directory of the batch file
cd /d "%~dp0"

:: Keep credentials and saved progress outside the OneDrive-backed project.
set KARYAKEEPER_DATA_DIR=%USERPROFILE%\.karyakeeper
set KARYAKEEPER_CONFIG_FILE=%KARYAKEEPER_DATA_DIR%\.env
IF NOT EXIST "%KARYAKEEPER_DATA_DIR%" mkdir "%KARYAKEEPER_DATA_DIR%"
IF EXIST ".env" IF NOT EXIST "%KARYAKEEPER_CONFIG_FILE%" move /Y ".env" "%KARYAKEEPER_CONFIG_FILE%" >nul

echo ===================================================
echo KaryaKeeper Automation - Runner
echo ===================================================
echo.

:: Use the same Python detection as setup.bat so pip and the runner always match
set PY_CMD=python
python --version >nul 2>&1
IF %ERRORLEVEL% NEQ 0 set PY_CMD=py -3
%PY_CMD% --version >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: Python is not installed or not added to the system PATH.
    echo Please run setup.bat first.
    pause
    exit /b 1
)

:: Must match the browser location used by setup.bat
set PLAYWRIGHT_BROWSERS_PATH=%USERPROFILE%\.karyakeeper-browsers

IF NOT EXIST "%PLAYWRIGHT_BROWSERS_PATH%" (
    echo ERROR: The Playwright browser is not installed yet.
    echo Please run setup.bat first.
    pause
    exit /b 1
)

IF NOT EXIST "%KARYAKEEPER_CONFIG_FILE%" (
    echo ERROR: The local configuration file is missing!
    echo Please run setup.bat first to configure the application.
    pause
    exit /b 1
)

:: Pre-seed Streamlit's credentials file so its first-run "enter your email" prompt
:: never appears and blocks this window waiting for input
IF NOT EXIST "%USERPROFILE%\.streamlit" mkdir "%USERPROFILE%\.streamlit"
IF NOT EXIST "%USERPROFILE%\.streamlit\credentials.toml" (
    (
        echo [general]
        echo email = ""
    ) > "%USERPROFILE%\.streamlit\credentials.toml"
)

:: Clear any sign-in session files left behind by older versions or crashes.
:: The app itself keeps sessions in memory only and never writes them to disk.
if exist "auth.json" del /q "auth.json" >nul 2>&1
if exist "kk_auth.json" del /q "kk_auth.json" >nul 2>&1

echo.
echo Starting KaryaKeeper Automation web app...
echo A browser tab will open automatically. Close this window to stop the app.
echo Your sign-in sessions are cleared automatically when the app stops.
echo.

%PY_CMD% -m streamlit run app\streamlit_app.py --server.address 127.0.0.1

if exist "auth.json" del /q "auth.json" >nul 2>&1
if exist "kk_auth.json" del /q "kk_auth.json" >nul 2>&1

echo.
pause
