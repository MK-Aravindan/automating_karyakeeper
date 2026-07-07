import os
import re
import json
import shutil
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Resolve paths relative to THIS script's location, not the working directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
LEGACY_DATA_DIR = os.path.join(ROOT_DIR, "app_data")


def _default_data_dir():
    override = os.getenv("KARYAKEEPER_DATA_DIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    if os.name == "nt":
        return os.path.join(os.getenv("USERPROFILE", os.path.expanduser("~")), ".karyakeeper")
    return os.path.join(os.path.expanduser("~"), ".karyakeeper")


DATA_DIR = _default_data_dir()
CONFIG_FILE = os.path.abspath(os.path.expanduser(
    os.getenv("KARYAKEEPER_CONFIG_FILE", os.path.join(DATA_DIR, ".env"))
))
STATE_FILE = os.path.join(DATA_DIR, "state.json")


class IncompleteHistoricalAttendanceError(ValueError):
    """Raised when an old attendance day has an IN punch without an OUT punch."""


class StateStoreError(RuntimeError):
    """Raised when saved local state cannot be read or safely written."""


def _move_local_file(source, destination):
    """Move a legacy local file after ensuring the destination is durable."""
    if not source or not os.path.exists(source) or os.path.abspath(source) == os.path.abspath(destination):
        return False
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    if os.path.exists(destination):
        try:
            with open(source, "rb") as src, open(destination, "rb") as dst:
                if src.read() == dst.read():
                    os.remove(source)
        except OSError:
            pass
        return False
    try:
        os.replace(source, destination)
    except OSError:
        shutil.copy2(source, destination)
        with open(source, "rb") as src, open(destination, "rb") as dst:
            if src.read() != dst.read():
                raise StateStoreError(f"Could not verify migration from {source} to {destination}.")
        os.remove(source)
    return True


def ensure_local_storage():
    """Keep credentials and progress out of the OneDrive-backed project folder."""
    os.makedirs(DATA_DIR, exist_ok=True)
    migrated = []
    prior_local_dir = (
        os.path.join(os.environ["LOCALAPPDATA"], "KaryaKeeper")
        if os.getenv("LOCALAPPDATA")
        else None
    )
    candidates = [
        (os.path.join(ROOT_DIR, ".env"), CONFIG_FILE, "configuration"),
        (os.path.join(LEGACY_DATA_DIR, "state.json"), STATE_FILE, "timesheet progress"),
        (os.path.join(ROOT_DIR, ".streamlit_state.json"), STATE_FILE, "legacy timesheet progress"),
    ]
    if prior_local_dir:
        candidates.extend([
            (os.path.join(prior_local_dir, ".env"), CONFIG_FILE, "configuration"),
            (os.path.join(prior_local_dir, "state.json"), STATE_FILE, "timesheet progress"),
        ])
    for source, destination, label in candidates:
        if _move_local_file(source, destination):
            migrated.append(label)
    return migrated


def load_json_with_backup(path):
    """Load JSON and recover from the last backup instead of silently discarding data."""
    if not os.path.exists(path):
        return {}, False
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), False
    except (OSError, json.JSONDecodeError, UnicodeError) as primary_error:
        backup = path + ".bak"
        try:
            with open(backup, "r", encoding="utf-8") as f:
                return json.load(f), True
        except (OSError, json.JSONDecodeError, UnicodeError) as backup_error:
            raise StateStoreError(
                f"Saved progress is unreadable ({primary_error}). Backup recovery also failed ({backup_error})."
            ) from primary_error


def atomic_write_json(path, data):
    """Durably replace a JSON file while retaining its previous valid version."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix="state-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as current:
                    json.load(current)
                shutil.copy2(path, path + ".bak")
            except (OSError, json.JSONDecodeError, UnicodeError):
                # Preserve the existing valid backup when the primary is corrupt.
                pass
        os.replace(temp_path, path)
    except Exception as error:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise StateStoreError(f"Could not save progress safely: {error}") from error


def round_dt_15_mins(dt):
    if not dt:
        return None
    minute = (dt.minute // 15) * 15
    if dt.minute % 15 > 10:
        minute += 15
    dt_rounded = dt.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=minute)
    return dt_rounded


def floor_dt_15_mins(dt):
    return dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)


def ceil_dt_15_mins(dt):
    floored = floor_dt_15_mins(dt)
    return floored if dt == floored else floored + timedelta(minutes=15)


def parse_punch_dt(dt_str):
    """Safely parse a punchDateTime string, stripping milliseconds if present."""
    if not dt_str:
        return None
    if "." in dt_str:
        dt_str = dt_str.split(".")[0]
    try:
        return datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
    except ValueError:
        return None


def swipe_sessions(swipes):
    """Pairs raw swipes into (in_dt, out_dt|None) work sessions for display,
    mirroring the attendance printout of the CLI version."""
    sessions = []
    current_in = None
    for swipe in sorted(swipes, key=lambda x: x.get('punchDateTime', '')):
        pt = parse_punch_dt(swipe.get('punchDateTime'))
        if pt is None:
            continue
        if swipe.get('inOutIndicator') == 1:
            current_in = pt
        elif current_in is not None:
            sessions.append((current_in, pt))
            current_in = None
    if current_in is not None:
        sessions.append((current_in, None))
    return sessions


def consolidate_blocks(swipes):
    blocks = []
    current_in = None
    current_out = None

    for swipe in sorted(swipes, key=lambda x: x.get('punchDateTime', '')):
        pt = parse_punch_dt(swipe.get('punchDateTime'))
        if pt is None:
            continue
        indicator = swipe.get('inOutIndicator')

        if indicator == 1:  # IN
            if current_in is None:
                current_in = pt
            else:
                if current_out is not None:
                    diff = (pt - current_out).total_seconds() / 60.0
                    if diff > 15:
                        blocks.append((current_in, current_out))
                        current_in = pt
                    current_out = None
        else:  # OUT
            if current_in is not None:
                current_out = pt

    if current_in is not None:
        blocks.append((current_in, current_out))

    return blocks


def filter_existing_blocks(blocks, existing_times, target_date):
    """Subtract existing continuous intervals and return safe 15-minute blocks.

    Any partial 15-minute segment touched by an existing entry is excluded. This
    intentionally favours avoiding a duplicate log over filling a sub-15-minute gap.
    """
    target_day = datetime.strptime(target_date, "%Y-%m-%d").date()
    today_ist = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    covered_intervals = []
    for s_str, e_str in existing_times:
        try:
            s_time = datetime.strptime(f"{target_date} {s_str}", "%Y-%m-%d %H:%M")
            e_time = datetime.strptime(f"{target_date} {e_str}", "%Y-%m-%d %H:%M")
            if e_time <= s_time:
                e_time += timedelta(days=1)
            covered_intervals.append((s_time, e_time))
        except (TypeError, ValueError):
            continue

    covered_intervals.sort(key=lambda interval: interval[0])

    filtered_blocks = []

    for start_dt, end_dt in blocks:
        start_r = round_dt_15_mins(start_dt)
        if end_dt:
            end_r = round_dt_15_mins(end_dt)
            is_running = False
        else:
            if target_day != today_ist:
                raise IncompleteHistoricalAttendanceError(
                    f"Attendance for {target_date} has an IN punch at {start_dt.strftime('%H:%M')} "
                    "without a matching OUT punch. Enter the missing OUT punch in GreytHR or use a manual entry."
                )
            now_ist = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
            end_r = round_dt_15_mins(now_ist)
            is_running = True

        if end_r <= start_r:
            end_r = start_r + timedelta(minutes=15)

        residuals = [(start_r, end_r)]
        for covered_start, covered_end in covered_intervals:
            next_residuals = []
            for residual_start, residual_end in residuals:
                if covered_end <= residual_start or covered_start >= residual_end:
                    next_residuals.append((residual_start, residual_end))
                    continue
                if covered_start > residual_start:
                    next_residuals.append((residual_start, covered_start))
                if covered_end < residual_end:
                    next_residuals.append((covered_end, residual_end))
            residuals = next_residuals
            if not residuals:
                break

        for residual_start, residual_end in residuals:
            safe_start = ceil_dt_15_mins(residual_start)
            safe_end = floor_dt_15_mins(residual_end)
            if safe_end - safe_start < timedelta(minutes=15):
                continue
            running_flag = is_running and safe_end == end_r
            filtered_blocks.append((safe_start, safe_end, running_flag))

    return filtered_blocks


def process_and_chunk_blocks(filtered_blocks):
    chunked = []
    for start_r, end_r, is_running in filtered_blocks:
        curr_start = start_r
        while True:
            diff_hours = (end_r - curr_start).total_seconds() / 3600.0
            if diff_hours <= 0:
                break
            if diff_hours <= 3.0:
                chunked.append((curr_start, end_r, is_running))
                break
            else:
                chunk_end = curr_start + timedelta(hours=3)
                chunked.append((curr_start, chunk_end, False))
                curr_start = chunk_end
    return chunked


def parse_clock_interval(target_date, start_time, end_time):
    start = datetime.strptime(f"{target_date} {start_time}", "%Y-%m-%d %H:%M")
    end = datetime.strptime(f"{target_date} {end_time}", "%Y-%m-%d %H:%M")
    if end <= start:
        end += timedelta(days=1)
    return start, end


def intervals_overlap(first_start, first_end, second_start, second_end):
    return first_start < second_end and second_start < first_end


def find_interval_conflict(target_date, start_time, end_time, intervals):
    candidate_start, candidate_end = parse_clock_interval(target_date, start_time, end_time)
    for label, other_start, other_end in intervals:
        try:
            parsed_start, parsed_end = parse_clock_interval(target_date, other_start, other_end)
        except (TypeError, ValueError):
            continue
        if intervals_overlap(candidate_start, candidate_end, parsed_start, parsed_end):
            return label
    return None


def total_interval_minutes(target_date, intervals):
    """Return union duration so overlapping rows are never double-counted in summaries."""
    parsed = []
    for start_time, end_time in intervals:
        try:
            parsed.append(parse_clock_interval(target_date, start_time, end_time))
        except (TypeError, ValueError):
            continue
    if not parsed:
        return 0
    parsed.sort(key=lambda interval: interval[0])
    merged = [list(parsed[0])]
    for start, end in parsed[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return int(sum((end - start).total_seconds() for start, end in merged) // 60)


def validate_date(date_str):
    """Validate that the date string is in YYYY-MM-DD format."""
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def login_karyakeeper(context, page, kk_url, kk_user, kk_pass):
    """Logs into KaryaKeeper, reusing the context's session if the page already
    shows the Dashboard. Raises on failure. The session lives only in browser
    memory — nothing is written to disk."""
    page.goto(kk_url, timeout=30000)

    # Wait for whichever appears first instead of burning a fixed 5s probe
    dashboard = page.locator("text=Dashboard").first
    login_box = page.locator("#login-email, input[name='email']").first
    dashboard.or_(login_box).first.wait_for(state="visible", timeout=30000)
    if dashboard.is_visible():
        return

    login_box.fill(kk_user)
    page.locator("#login-password, input[name='password']").first.fill(kk_pass)
    page.keyboard.press("Enter")

    page.wait_for_selector("text=Dashboard", timeout=30000)


def parse_logged_description(desc):
    """KaryaKeeper renders the description cell as 'Task : <task>\\n<remark>'."""
    parts = desc.split("\n", 1)
    header = parts[0].strip()
    remark = parts[1].strip() if len(parts) > 1 else ""
    task = header.split(":", 1)[1].strip() if ":" in header else header
    return task, remark


def fetch_existing_entries_detailed(page, kk_url, target_date):
    """Returns dicts for entries already logged on target_date, with keys
    project, task, remark, start, end. Table columns are:
    Project, Who, Description, Task Group, Start, End, Billable, Time, Action."""
    page.goto(kk_url.rstrip('/') + "/timesheet", wait_until="load", timeout=30000)

    # A logged-out session lands on the login page whose empty body would silently
    # read as "no entries", risking double-logging — fail loudly instead
    if page.locator("#login-email, input[name='email']").count():
        raise RuntimeError("KaryaKeeper session expired.")

    target_date_str = datetime.strptime(target_date, "%Y-%m-%d").strftime("%d %B %Y")

    js_code = f"""
    () => {{
        let targetRows = [];
        document.querySelectorAll('.table-responsive-md').forEach(div => {{
            let dateText = div.previousElementSibling ? div.previousElementSibling.innerText : '';
            if (dateText.includes('{target_date_str}')) {{
                div.querySelectorAll('tbody tr').forEach(tr => {{
                    targetRows.push(Array.from(tr.querySelectorAll('td')).map(td => td.innerText.trim()));
                }});
            }}
        }});
        return targetRows;
    }}
    """
    rows = page.evaluate(js_code)

    entries = []
    for r in rows:
        if len(r) >= 6 and re.match(r"^\d{2}:\d{2}$", r[4]) and re.match(r"^\d{2}:\d{2}$", r[5]):
            task, remark = parse_logged_description(r[2])
            group = r[3].strip()
            entries.append({
                "project": r[0],
                "task": f"[{group}] {task}" if group and task else (task or group),
                "remark": remark,
                "start": r[4],
                "end": r[5],
            })
    return entries


def fetch_existing_entries(page, kk_url, target_date):
    """Returns a list of (start, end) HH:MM strings already logged for target_date."""
    return [(e["start"], e["end"]) for e in fetch_existing_entries_detailed(page, kk_url, target_date)]


OPTIONS_JS = "els => els.map(e => ({text: e.innerText.trim(), value: e.value})).filter(e => e.value)"


def fetch_projects(page, kk_url):
    """Navigates to the timesheet creation form and returns the available projects."""
    page.goto(kk_url.rstrip('/') + "/timesheet?action=create", wait_until="domcontentloaded", timeout=30000)
    # select2 hides the raw <select>, so wait for attachment, not visibility
    page.wait_for_selector("#logProjects option", state="attached", timeout=30000)
    projects = page.locator("#logProjects option").evaluate_all(OPTIONS_JS)
    if not projects:
        page.wait_for_function(
            "document.querySelectorAll('#logProjects option[value]:not([value=\"\"])').length > 0",
            timeout=10000,
        )
        projects = page.locator("#logProjects option").evaluate_all(OPTIONS_JS)
    return projects


def fetch_tasks(page, kk_url, project_id):
    """Returns the list of tasks for the given project id. Raises on a non-OK response."""
    res = page.request.get(
        f"{kk_url.rstrip('/')}/project/timesheet/task",
        params={"projectId": str(project_id)},
        timeout=30000,
    )
    if not res.ok:
        raise RuntimeError(f"Failed to fetch tasks (HTTP {res.status}).")
    return res.json().get("results", [])


def fill_and_submit_timesheet_form(page, kk_date, start_time, end_time, remark, project_id, task_id, task_title):
    """Fills the timesheet creation form and clicks the initial submit button. The page
    must already be on the '/timesheet?action=create' form before calling this."""
    page.evaluate(
        """
        values => {
            const setValue = (id, value) => {
                const element = document.getElementById(id);
                if (!element) throw new Error(`Missing form field: ${id}`);
                element.value = value;
                element.dispatchEvent(new Event('input', { bubbles: true }));
                element.dispatchEvent(new Event('change', { bubbles: true }));
            };
            setValue('date', values.date);
            setValue('start_time', values.start);
            setValue('end_time', values.end);
            setValue('remark', values.remark);

            const taskOption = new Option(values.taskTitle, String(values.taskId), true, true);
            $('#logTasks').append(taskOption).trigger('change');
            $('#logProjects').val(String(values.projectId)).trigger('change');
        }
        """,
        {
            "date": kk_date,
            "start": start_time,
            "end": end_time,
            "remark": remark,
            "projectId": project_id,
            "taskId": task_id,
            "taskTitle": task_title,
        },
    )

    page.locator("#submit_timesheet").click()
    # Wait for the confirmation dialog instead of sleeping a fixed 1.5s
    page.locator("#log_button").wait_for(state="visible", timeout=20000)


def confirm_timesheet_log(page):
    """Clicks the final confirmation button that actually commits the entry."""
    confirm = page.locator("#log_button")
    confirm.click()
    confirm.wait_for(state="hidden", timeout=30000)
    return True
