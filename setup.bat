@echo off
setlocal

:: Change working directory to the directory of the batch file
cd /d "%~dp0"

:: Keep credentials and saved progress outside the OneDrive-backed project.
set KARYAKEEPER_DATA_DIR=%USERPROFILE%\.karyakeeper
set KARYAKEEPER_CONFIG_FILE=%KARYAKEEPER_DATA_DIR%\.env
IF NOT EXIST "%KARYAKEEPER_DATA_DIR%" mkdir "%KARYAKEEPER_DATA_DIR%"

echo ===================================================
echo KaryaKeeper Automation - Setup
echo ===================================================
echo.

:: 1. Check Python (try "python" first, then fall back to the "py" launcher)
echo [1/4] Checking Python installation...
set PY_CMD=python
python --version >nul 2>&1
IF %ERRORLEVEL% NEQ 0 set PY_CMD=py -3
%PY_CMD% --version >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: Python is not installed or not added to the system PATH.
    echo Please install Python 3.9 or newer from the Company Portal, python.org
    echo or the Microsoft Store. If using the python.org installer, make sure you
    echo check the box "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)
%PY_CMD% -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: Python 3.9 or newer is required, but an older version was found:
    %PY_CMD% --version
    pause
    exit /b 1
)
%PY_CMD% --version
echo Python is successfully installed.
echo.

:: Note: Python Playwright bundles its own Node.js executable,
:: so a separate system-wide Node.js installation is not required.

:: 2. Install Python Requirements
echo [2/4] Installing Python dependencies (this may take a minute)...
%PY_CMD% -m pip install -q -r app\requirements.txt
IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: Failed to install Python dependencies.
    pause
    exit /b 1
)
echo Dependencies installed successfully.
echo.

:: 3. Install Playwright Chromium
echo [3/4] Installing Playwright (Chromium)...

:: Browsers must live outside OneDrive (sync breaks Playwright's download lock) and outside AppData (blocked by AppLocker on corporate machines)
set PLAYWRIGHT_BROWSERS_PATH=%USERPROFILE%\.karyakeeper-browsers

:: Old versions downloaded browsers into the project folder; remove that copy since it breaks and bloats OneDrive sync
IF EXIST "%~dp0.browsers" (
    echo Removing old .browsers folder from the project directory...
    rd /s /q "%~dp0.browsers"
)

:: Clear any stale download lock left behind by a previously failed install
IF EXIST "%PLAYWRIGHT_BROWSERS_PATH%\__dirlock" (
    rd /s /q "%PLAYWRIGHT_BROWSERS_PATH%\__dirlock" 2>nul
    del /f /q "%PLAYWRIGHT_BROWSERS_PATH%\__dirlock" 2>nul
)

set RETRIED=
:install_browsers
%PY_CMD% -m playwright install chromium
IF %ERRORLEVEL% EQU 0 goto browsers_done
IF NOT DEFINED RETRIED (
    set RETRIED=1
    echo.
    echo Browser download failed. Retrying once...
    echo.
    goto install_browsers
)
echo ERROR: Failed to install Playwright browsers.
echo If you are on a VPN or corporate proxy, try again after disconnecting.
pause
exit /b 1
:browsers_done
echo Playwright installed successfully.
echo.

:: 4. Configure the local .env File
echo [4/4] Checking configuration file...
set "FRESH_CONFIG="
IF EXIST ".env" IF NOT EXIST "%KARYAKEEPER_CONFIG_FILE%" (
    echo Moving the existing configuration out of the OneDrive project folder...
    move /Y ".env" "%KARYAKEEPER_CONFIG_FILE%" >nul
)
IF NOT EXIST "%KARYAKEEPER_CONFIG_FILE%" (
    echo Creating .env file template...
    (
        echo GREYTHR_DOMAIN=7dxperts
        echo GREYTHR_USERNAME=
        echo GREYTHR_PASSWORD=
        echo KARYAKEEPER_URL=https://app.karyakeeper.com/
        echo KARYAKEEPER_USERNAME=
        echo KARYAKEEPER_PASSWORD=
    ) > "%KARYAKEEPER_CONFIG_FILE%"
    set FRESH_CONFIG=1
) ELSE (
    echo Local configuration already exists.
)

echo.
:: A freshly created template still has blank credentials. Do not claim success;
:: open it and make the required action impossible to miss, even if the user
:: closes Notepad, because the terminal window stays open on the pause below.
IF DEFINED FRESH_CONFIG (
    start "" notepad "%KARYAKEEPER_CONFIG_FILE%"
    echo ============================================================
    echo   ACTION REQUIRED - you must enter your credentials
    echo ============================================================
    echo A configuration file has opened in Notepad:
    echo   "%KARYAKEEPER_CONFIG_FILE%"
    echo.
    echo Type your GreytHR and KaryaKeeper username and password,
    echo then save the file and close Notepad.
    echo.
    echo If you closed Notepad by mistake, open that same file again,
    echo fill in the values and save. The app will not start until all
    echo six values are filled in.
    echo ============================================================
) ELSE (
    echo ===================================================
    echo Setup is complete!
    echo You can now run the application using 'run.bat'.
    echo ===================================================
)
pause
