import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Resolve paths relative to THIS script's location, not the working directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
KK_AUTH_PATH = os.path.join(ROOT_DIR, "kk_auth.json")


def round_dt_15_mins(dt):
    if not dt:
        return None
    minute = (dt.minute // 15) * 15
    if dt.minute % 15 > 10:
        minute += 15
    dt_rounded = dt.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=minute)
    return dt_rounded


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
    covered_segments = set()
    for s_str, e_str in existing_times:
        try:
            s_time = datetime.strptime(f"{target_date} {s_str}", "%Y-%m-%d %H:%M")
            e_time = datetime.strptime(f"{target_date} {e_str}", "%Y-%m-%d %H:%M")
            curr = s_time
            while curr < e_time:
                covered_segments.add(curr)
                curr += timedelta(minutes=15)
        except Exception:
            pass

    filtered_blocks = []

    for start_dt, end_dt in blocks:
        start_r = round_dt_15_mins(start_dt)
        if end_dt:
            end_r = round_dt_15_mins(end_dt)
            is_running = False
        else:
            now_ist = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
            end_r = round_dt_15_mins(now_ist)
            is_running = True

        if end_r <= start_r:
            end_r = start_r + timedelta(minutes=15)

        uncovered = []
        curr = start_r
        while curr < end_r:
            if curr not in covered_segments:
                uncovered.append(curr)
            curr += timedelta(minutes=15)

        if not uncovered:
            continue

        uncovered.sort()
        curr_start = uncovered[0]
        prev = uncovered[0]

        for seg in uncovered[1:]:
            if seg == prev + timedelta(minutes=15):
                prev = seg
            else:
                filtered_blocks.append((curr_start, prev + timedelta(minutes=15), False))
                curr_start = seg
                prev = seg

        final_end = prev + timedelta(minutes=15)
        running_flag = is_running if final_end == end_r else False
        filtered_blocks.append((curr_start, final_end, running_flag))

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


def cleanup_auth_files():
    """Remove session files from the root project directory."""
    for f in ["auth.json", "kk_auth.json"]:
        full_path = os.path.join(ROOT_DIR, f)
        if os.path.exists(full_path):
            try:
                os.remove(full_path)
            except Exception:
                pass


def validate_date(date_str):
    """Validate that the date string is in YYYY-MM-DD format."""
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def js_safe(s):
    """Escape a string safely for inline JS single-quote context."""
    return s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ").replace("\r", "")


def login_karyakeeper(context, page, kk_url, kk_user, kk_pass):
    """Logs into KaryaKeeper, reusing an existing session if the page already loads the Dashboard.
    Raises on failure. Saves a fresh session to KK_AUTH_PATH on a successful new login."""
    page.goto(kk_url, timeout=30000)

    try:
        page.wait_for_selector("text=Dashboard", timeout=5000)
        return
    except Exception:
        pass

    page.locator("#login-email, input[name='email']").first.fill(kk_user)
    page.locator("#login-password, input[name='password']").first.fill(kk_pass)
    page.keyboard.press("Enter")

    page.wait_for_selector("text=Dashboard", timeout=30000)
    context.storage_state(path=KK_AUTH_PATH)


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
    page.goto(kk_url.rstrip('/') + "/timesheet")
    page.wait_for_load_state("networkidle")

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


def fetch_projects(page, kk_url):
    """Navigates to the timesheet creation form and returns the available projects."""
    page.goto(kk_url.rstrip('/') + "/timesheet?action=create")
    page.wait_for_load_state("networkidle")
    return page.locator("#logProjects option").evaluate_all(
        "els => els.map(e => ({text: e.innerText.trim(), value: e.value})).filter(e => e.value)"
    )


def fetch_tasks(page, kk_url, project_id):
    """Returns the list of tasks for the given project id. Raises on a non-OK response."""
    res = page.request.get(f"{kk_url.rstrip('/')}/project/timesheet/task?projectId={project_id}")
    if not res.ok:
        raise RuntimeError(f"Failed to fetch tasks (HTTP {res.status}).")
    return res.json().get("results", [])


def fill_and_submit_timesheet_form(page, kk_date, start_time, end_time, remark, project_id, task_id, task_title):
    """Fills the timesheet creation form and clicks the initial submit button. The page
    must already be on the '/timesheet?action=create' form before calling this."""
    remark_esc = js_safe(remark)
    task_title_esc = js_safe(task_title)

    page.evaluate(f"""
        document.getElementById('date').value = '{kk_date}';
        document.getElementById('start_time').value = '{start_time}';
        document.getElementById('end_time').value = '{end_time}';
        document.getElementById('remark').value = '{remark_esc}';

        let taskOption = new Option('{task_title_esc}', '{task_id}', true, true);
        $('#logTasks').append(taskOption).trigger('change');
        $('#logProjects').val('{project_id}').trigger('change');
    """)

    page.locator("#submit_timesheet").click()
    page.wait_for_timeout(1500)


def confirm_timesheet_log(page):
    """Clicks the final confirmation button that actually commits the entry."""
    page.locator("#log_button").click()
    page.wait_for_timeout(3000)
