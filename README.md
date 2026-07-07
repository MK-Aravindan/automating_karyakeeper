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
3. Finally, it will generate a private local configuration at `%USERPROFILE%\.karyakeeper\.env` and automatically open it in **Notepad**. Existing project-level configuration is moved there automatically so it is not stored in OneDrive.
4. Fill in your GreytHR and KaryaKeeper usernames and passwords in the Notepad window.
5. **Save** the file (File -> Save) and close Notepad.
6. The setup is now complete! You will not need to run `setup.bat` again unless you install Python on a new computer or update this tool.

## Daily Usage
1. At the end of your workday, simply double-click the **`run.bat`** file.
2. A terminal window opens a local web app and automatically opens it in your browser (usually at `http://127.0.0.1:8501`). The server is bound to your computer only and is not exposed to the local network. Keep the terminal window open while you use the app; closing it stops the app.
3. Today's attendance is fetched automatically as soon as the app opens — step-by-step status messages show progress as it reads your GreytHR attendance, logs into KaryaKeeper, and compares against what you've already logged. To log a different date, pick it and click **Fetch Attendance**. The first fetch after starting the app takes the longest because it signs into both sites; after that the sessions stay active in the background, so loading tasks, saving entries, and fetching other dates are all much faster.
4. The page then shows everything in order, just like the old terminal output:
   - **Summary** — attendance, already logged, remaining time, and completion count at a glance.
   - **Entries to complete** — the main work area appears first, with one card per unlogged time block (max 3 hours each).
   - **Attendance details** and **Already Logged in KaryaKeeper** — full reference tables shown directly below the entries.
   - Entry tools include:
     - **Start / End time**: Editable time fields. Changing a start time automatically re-splits the rows below into fresh 3-hour blocks up to your actual clock-out time. Changing an end time shortens/extends just that row and reflows the rows after it the same way.
     - **Project / Task**: Pick from dropdowns. The task list loads automatically once you pick a project.
     - **Remark**: A short description of what you worked on.
     - **Skip / Restore**: Keeps a row local without submitting it.
     - **Add manual entry**: Adds a missing block without changing attendance data.
     - **Save / Save all ready**: Saves one completed row or all currently valid rows. Every row is validated for duration and overlap first.
5. Once an entry is saved, it locks (shown with a green "Saved" badge) so it can't be accidentally double-logged. Saved entries are safe in KaryaKeeper itself, and your typed progress is kept in `%USERPROFILE%\.karyakeeper` while you work.
6. **State is remembered separately for each date.** Switching the date picker back to an earlier date you already worked on restores exactly what you had there — no need to re-fetch.
7. **Refresh attendance** is a hard refresh: it discards the selected date's local draft, re-validates the sign-in sessions (logging in again automatically if they expired), and reloads attendance, logged entries, and all calculated totals fresh. Opening the app does the same for today automatically, so unsaved draft rows do not survive a refresh or an app restart.

## Troubleshooting
- **`Error: Unable to update lock within the stale threshold ... __dirlock`** during setup: this happened in older versions because the browser was downloaded into the project folder, and OneDrive sync corrupted the download lock. Pull the latest version of this tool and re-run `setup.bat` — it now downloads outside OneDrive and cleans up the old broken folder automatically.
- **`run.bat` says the browser is not installed**: run `setup.bat` once (also required after updating this tool, since the browser location changed).
- **The browser download fails or stalls**: `setup.bat` retries once automatically; if it still fails, disconnect from VPN/proxy and run `setup.bat` again.
- **`No module named 'playwright'` or `No module named 'streamlit'`**: re-run `setup.bat`. Both scripts now resolve Python the same way, so dependencies are always installed for the same Python that runs the app.
- **A browser tab didn't open automatically**: open `http://127.0.0.1:8501` manually — the address is also printed in the terminal window.
- **`Port 8501 is already in use`**: you already have this app (or another Streamlit app) running in another window. Close that window first, or finish using it there instead.

## Security Note
Credentials and timesheet progress are stored outside the OneDrive project in `%USERPROFILE%\.karyakeeper`. While the app is open it keeps GreytHR and KaryaKeeper sign-in sessions **in memory only**; those sessions disappear when the app closes. The web server listens only on `127.0.0.1`, so other computers on the network cannot open it. `run.bat` also deletes session files left by older versions.
