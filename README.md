# KaryaKeeper Automation

This tool automates the process of logging your daily timesheets into KaryaKeeper. It automatically reads your biometric attendance swipes from GreytHR, accurately calculates your working hours, removes any time you have already logged, and intelligently chunks the remaining time to ensure compliance with KaryaKeeper's 3-hour per-entry limit. 

## Prerequisites
- **Python 3.9 or newer**: If you do not have Python installed, install it from the **Company Portal**, the **Microsoft Store**, or [python.org](https://www.python.org/downloads/).
- **Important**: If you use the python.org installer, make sure you check the box that says **"Add Python to PATH"**. (The scripts also find Python through the `py` launcher automatically if PATH was not set.)

## First-Time Setup
1. Simply double-click the **`setup.bat`** file.
2. The script will automatically:
   - Verify your Python installation.
   - Install the required background dependencies.
   - Download the headless Chromium browser into `C:\Users\<you>\.karyakeeper-browsers`. This folder is deliberately outside OneDrive (sync corrupts the download) and outside AppData (blocked on some corporate machines). It is safe to delete; just re-run `setup.bat` afterwards.
3. Finally, it will generate a `.env` configuration file and automatically open it for you in **Notepad**.
4. Fill in your GreytHR and KaryaKeeper usernames and passwords in the Notepad window.
5. **Save** the file (File -> Save) and close Notepad.
6. The setup is now complete! You will not need to run `setup.bat` again unless you install Python on a new computer or update this tool.

## Daily Usage
1. At the end of your workday, simply double-click the **`run.bat`** file.
2. A terminal window opens a local web app and automatically opens it in your browser (usually at `http://localhost:8501`). Keep the terminal window open while you use the app; closing it stops the app.
3. Pick the date you want to log (defaults to today) and click **Fetch Attendance**. Step-by-step status messages show progress as it reads your GreytHR attendance, logs into KaryaKeeper, and compares against what you've already logged.
4. The page then shows everything in order, just like the old terminal output:
   - **Attendance** — your in/out swipes with durations and the breaks between them, plus your total time worked.
   - **Already Logged in KaryaKeeper** — the time ranges that already exist for that date.
   - **New Entries to Log** — one card per unlogged time block (max 3 hours each):
     - **Start / End time**: Editable time fields. Changing a start time automatically re-splits the rows below into fresh 3-hour blocks up to your actual clock-out time. Changing an end time shortens/extends just that row and reflows the rows after it the same way.
     - **Project / Task**: Pick from dropdowns. The task list loads automatically once you pick a project.
     - **Remark**: A short description of what you worked on.
     - **Save**: Becomes active once Project, Task, and Remark are filled in. Click it to log that entry to KaryaKeeper immediately — no need to fill in every entry first.
5. Once an entry is saved, it locks (shown with a green "Saved" badge) so it can't be accidentally double-logged. You can leave the app at any point — saved entries and everything you've typed stay saved on your machine and reload automatically if you refresh the page or close and reopen the app.
6. **State is remembered separately for each date.** Opening the app on a new day starts fresh for that day, while switching the date picker back to an earlier date you already worked on restores exactly what you had there — no need to re-fetch.
7. Use **Start Over** (next to the Fetch button) to discard the currently selected date's data and fetch it again. Other dates are left untouched.

## Troubleshooting
- **`Error: Unable to update lock within the stale threshold ... __dirlock`** during setup: this happened in older versions because the browser was downloaded into the project folder, and OneDrive sync corrupted the download lock. Pull the latest version of this tool and re-run `setup.bat` — it now downloads outside OneDrive and cleans up the old broken folder automatically.
- **`run.bat` says the browser is not installed**: run `setup.bat` once (also required after updating this tool, since the browser location changed).
- **The browser download fails or stalls**: `setup.bat` retries once automatically; if it still fails, disconnect from VPN/proxy and run `setup.bat` again.
- **`No module named 'playwright'` or `No module named 'streamlit'`**: re-run `setup.bat`. Both scripts now resolve Python the same way, so dependencies are always installed for the same Python that runs the app.
- **A browser tab didn't open automatically**: open `http://localhost:8501` manually — the address is also printed in the terminal window.
- **`Port 8501 is already in use`**: you already have this app (or another Streamlit app) running in another window. Close that window first, or finish using it there instead.

## Security Note
Your credentials are intentionally stored entirely locally on your computer inside the `.env` file. While the app is open it keeps a temporary KaryaKeeper session so repeated actions don't need to log in again and again, but that session is permanently deleted as soon as you close the app (the terminal window) to ensure maximum security.
