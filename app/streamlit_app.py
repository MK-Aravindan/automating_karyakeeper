import os
import sys
import json
import atexit
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Streamlit forces the Windows selector event loop, which cannot spawn subprocesses;
# Playwright needs the proactor loop to launch its browser driver.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import streamlit as st
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

import karyakeeper_core as kkc
from fetch_attendance import fetch_in_out_time

ROOT_DIR = kkc.ROOT_DIR
STATE_FILE = os.path.join(ROOT_DIR, ".streamlit_state.json")
DATE_KEYS = ("entries", "projects", "tasks_cache", "sessions", "existing_entries")

st.set_page_config(page_title="KaryaKeeper Automation", page_icon="🗓️", layout="wide")

st.markdown("""
<style>
.block-container {max-width: 1150px; padding-top: 2.2rem; margin: auto;}
.stButton button p {white-space: nowrap;}
</style>
""", unsafe_allow_html=True)

load_dotenv(os.path.join(ROOT_DIR, ".env"))
GT_DOMAIN = os.getenv("GREYTHR_DOMAIN")
GT_USER = os.getenv("GREYTHR_USERNAME")
GT_PASS = os.getenv("GREYTHR_PASSWORD")
KK_URL = os.getenv("KARYAKEEPER_URL")
KK_USER = os.getenv("KARYAKEEPER_USERNAME")
KK_PASS = os.getenv("KARYAKEEPER_PASSWORD")

missing = [k for k, v in {
    "GREYTHR_DOMAIN": GT_DOMAIN,
    "GREYTHR_USERNAME": GT_USER,
    "GREYTHR_PASSWORD": GT_PASS,
    "KARYAKEEPER_URL": KK_URL,
    "KARYAKEEPER_USERNAME": KK_USER,
    "KARYAKEEPER_PASSWORD": KK_PASS,
}.items() if not v]

if missing:
    st.error(f"Missing credentials in .env file: {', '.join(missing)}. Please run setup.bat and fill in the .env file.")
    st.stop()


def load_store():
    """Returns the full {date: state} map. Migrates the old single-date file if found."""
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    if "entries" in data:  # old single-date format, keyed by target_date inside
        td = data.get("target_date")
        return {td: {k: data.get(k) for k in DATE_KEYS}} if td else {}
    return data


def save_store(store):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(store, f)


def save_state():
    """Persist the active date's working data into the per-date store."""
    td = st.session_state.get("target_date")
    if not td:
        return
    store = load_store()
    store[td] = {k: st.session_state.get(k) for k in DATE_KEYS}
    save_store(store)


def clear_all_row_widget_keys():
    for k in list(st.session_state.keys()):
        if k.startswith(("start_", "end_", "project_", "task_", "remark_")):
            st.session_state.pop(k)


def blank_working_state():
    for k in DATE_KEYS:
        st.session_state[k] = {} if k == "tasks_cache" else []
    st.session_state["target_date"] = None


def normalize_sessions(sessions):
    """Convert any 12-hour times saved by older versions to 24-hour."""
    for s in sessions or []:
        for k in ("in", "out"):
            if s.get(k) and ("AM" in s[k] or "PM" in s[k]):
                s[k] = datetime.strptime(s[k], "%I:%M %p").strftime("%H:%M")
    return sessions


def switch_to_date(date_str):
    """Load a date's saved working data into the active session, or start blank if
    that date has not been fetched yet."""
    clear_all_row_widget_keys()
    saved = load_store().get(date_str)
    if not saved:
        blank_working_state()
        return
    st.session_state["entries"] = saved.get("entries") or []
    st.session_state["projects"] = saved.get("projects") or []
    st.session_state["tasks_cache"] = saved.get("tasks_cache") or {}
    st.session_state["sessions"] = normalize_sessions(saved.get("sessions") or [])
    st.session_state["existing_entries"] = saved.get("existing_entries") or []
    st.session_state["target_date"] = date_str


def reset_state():
    """Discard only the currently viewed date's saved data, keeping other dates."""
    td = st.session_state.get("target_date") or st.session_state.get("picker_date")
    if td:
        store = load_store()
        if td in store:
            del store[td]
            save_store(store)
    clear_all_row_widget_keys()
    blank_working_state()


if "initialized" not in st.session_state:
    blank_working_state()
    st.session_state["initialized"] = True
    atexit.register(kkc.cleanup_auth_files)


def fmt_minutes(m):
    h, mm = divmod(int(m), 60)
    if h and mm:
        return f"{h} hr {mm} min"
    if h:
        return f"{h} hr"
    return f"{mm} min"


def open_kk_context(browser):
    if os.path.exists(kkc.KK_AUTH_PATH):
        return browser.new_context(storage_state=kkc.KK_AUTH_PATH)
    return browser.new_context()


def build_session_rows(swipes):
    rows = []
    prev_out = None
    for in_dt, out_dt in kkc.swipe_sessions(swipes):
        rows.append({
            "in": in_dt.strftime("%H:%M"),
            "out": out_dt.strftime("%H:%M") if out_dt else None,
            "minutes": int((out_dt - in_dt).total_seconds() // 60) if out_dt else None,
            "break_min": int((in_dt - prev_out).total_seconds() // 60) if prev_out else None,
        })
        if out_dt:
            prev_out = out_dt
    return rows


def do_fetch_attendance(target_date):
    status = st.status("Fetching your timesheet data...", expanded=True)
    try:
        status.write("**Step 1/4** — Logging into GreytHR and reading your swipes...")
        attendance = fetch_in_out_time(GT_DOMAIN, GT_USER, GT_PASS, target_date)
        if not attendance or not attendance.get("swipes"):
            status.update(label="No attendance punches found for this date.", state="error")
            return

        blocks = kkc.consolidate_blocks(attendance["swipes"])
        if not blocks:
            status.update(label="Could not build any work blocks from the swipe data.", state="error")
            return

        status.write("**Step 2/4** — Logging into KaryaKeeper...")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = open_kk_context(browser)
                page = context.new_page()
                try:
                    kkc.login_karyakeeper(context, page, KK_URL, KK_USER, KK_PASS)
                except Exception as e:
                    status.update(label=f"Failed to log into KaryaKeeper: {e}", state="error")
                    return

                status.write("**Step 3/4** — Checking entries you've already logged for this date...")
                existing_entries = kkc.fetch_existing_entries_detailed(page, KK_URL, target_date)
                existing_times = [(e["start"], e["end"]) for e in existing_entries]

                filtered_blocks = kkc.filter_existing_blocks(blocks, existing_times, target_date)
                chunked_blocks = kkc.process_and_chunk_blocks(filtered_blocks)

                status.write("**Step 4/4** — Loading your project list...")
                projects = kkc.fetch_projects(page, KK_URL)
                if not projects and chunked_blocks:
                    status.update(label="No projects found in your KaryaKeeper account.", state="error")
                    return
            finally:
                browser.close()

        entries = [
            {
                "start": start_dt.strftime("%H:%M"),
                "end": end_dt.strftime("%H:%M"),
                "is_running": is_running,
                "project_id": None,
                "project_text": "",
                "task_id": None,
                "task_title": "",
                "remark": "",
                "saved": False,
            }
            for start_dt, end_dt, is_running in chunked_blocks
        ]

        st.session_state["target_date"] = target_date
        st.session_state["entries"] = entries
        st.session_state["projects"] = projects
        st.session_state["tasks_cache"] = {}
        st.session_state["sessions"] = build_session_rows(attendance["swipes"])
        st.session_state["existing_entries"] = existing_entries
        clear_all_row_widget_keys()
        save_state()

        if entries:
            status.update(label=f"Done — found {len(entries)} unlogged time block(s) for {target_date}.", state="complete", expanded=False)
        else:
            status.update(label="Done — all your time for this date is already logged.", state="complete", expanded=False)
    except Exception as e:
        status.update(label=f"Unexpected error: {e}", state="error")


def get_tasks_for_project(project_id):
    cache = st.session_state["tasks_cache"]
    if project_id in cache:
        return cache[project_id]

    try:
        with st.spinner("Loading tasks for this project..."):
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    context = open_kk_context(browser)
                    page = context.new_page()
                    kkc.login_karyakeeper(context, page, KK_URL, KK_USER, KK_PASS)
                    tasks = kkc.fetch_tasks(page, KK_URL, project_id)
                finally:
                    browser.close()
    except Exception as e:
        st.error(f"Failed to load tasks: {e}")
        return []

    cache[project_id] = tasks
    save_state()
    return tasks


def do_save_entry(i):
    entries = st.session_state["entries"]
    entry = entries[i]
    kk_date = datetime.strptime(st.session_state["target_date"], "%Y-%m-%d").strftime("%d/%m/%Y")

    try:
        with st.spinner(f"Saving entry {i + 1} to KaryaKeeper..."):
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    context = open_kk_context(browser)
                    page = context.new_page()
                    kkc.login_karyakeeper(context, page, KK_URL, KK_USER, KK_PASS)
                    page.goto(KK_URL.rstrip('/') + "/timesheet?action=create")
                    page.wait_for_load_state("networkidle")
                    kkc.fill_and_submit_timesheet_form(
                        page, kk_date, entry["start"], entry["end"], entry["remark"],
                        entry["project_id"], entry["task_id"], entry["task_title"],
                    )
                    kkc.confirm_timesheet_log(page)
                finally:
                    browser.close()
    except Exception as e:
        st.error(f"Failed to save entry {i + 1}: {e}")
        return

    entry["saved"] = True
    st.session_state["existing_entries"] = (st.session_state.get("existing_entries") or []) + [{
        "project": entry["project_text"],
        "task": entry["task_title"],
        "remark": entry["remark"],
        "start": entry["start"],
        "end": entry["end"],
    }]
    save_state()
    st.toast(f"Entry {i + 1} logged to KaryaKeeper.", icon="✅")


def rebuild_chain(start_dt, end_dt, preserve_running):
    """Re-chunk (start_dt, end_dt) into <=3h pieces, the same rule used for the initial fetch."""
    chunked = kkc.process_and_chunk_blocks([(start_dt, end_dt, preserve_running)])
    return [
        {
            "start": s.strftime("%H:%M"),
            "end": e.strftime("%H:%M"),
            "is_running": r,
            "project_id": None,
            "project_text": "",
            "task_id": None,
            "task_title": "",
            "remark": "",
            "saved": False,
        }
        for s, e, r in chunked
    ]


def row_is_locked_by_later_save(i):
    entries = st.session_state["entries"]
    return any(e["saved"] for e in entries[i + 1:])


def clear_row_widget_keys(start_idx, end_idx):
    """Widgets keep showing their last session_state value even if a new `value=` is
    passed on rerun, so any row rebuilt programmatically needs its widget keys cleared
    to force them to pick up the freshly computed values."""
    for idx in range(start_idx, end_idx):
        for prefix in ("start_", "end_", "project_", "task_", "remark_"):
            st.session_state.pop(f"{prefix}{idx}", None)


def apply_start_edit(i, new_start_str):
    entries = st.session_state["entries"]
    target_date = st.session_state["target_date"]
    anchor_end_dt = datetime.strptime(f"{target_date} {entries[-1]['end']}", "%Y-%m-%d %H:%M")
    new_start_dt = datetime.strptime(f"{target_date} {new_start_str}", "%Y-%m-%d %H:%M")

    if new_start_dt >= anchor_end_dt:
        st.session_state["edit_error"] = "Start time must be before the day's final end time."
        return

    was_running = entries[-1]["is_running"]
    old_len = len(entries)
    # Only rebuild the rows AFTER i from scratch; row i keeps whatever project/task/remark
    # it already had, since only its time changed, not its assignment.
    chain = rebuild_chain(new_start_dt, anchor_end_dt, was_running)
    entries[i]["start"] = chain[0]["start"]
    entries[i]["end"] = chain[0]["end"]
    entries[i]["is_running"] = chain[0]["is_running"]
    entries[i + 1:] = chain[1:]

    clear_row_widget_keys(i, max(old_len, len(entries)))
    save_state()


def apply_end_edit(i, new_end_str):
    entries = st.session_state["entries"]
    target_date = st.session_state["target_date"]
    row_start_dt = datetime.strptime(f"{target_date} {entries[i]['start']}", "%Y-%m-%d %H:%M")
    new_end_dt = datetime.strptime(f"{target_date} {new_end_str}", "%Y-%m-%d %H:%M")

    if new_end_dt <= row_start_dt:
        st.session_state["edit_error"] = "End time must be after this row's start time."
        return

    is_last_row = i == len(entries) - 1
    if is_last_row:
        entries[i]["end"] = new_end_str
        entries[i]["is_running"] = False
        save_state()
        return

    anchor_end_dt = datetime.strptime(f"{target_date} {entries[-1]['end']}", "%Y-%m-%d %H:%M")
    was_running = entries[-1]["is_running"]
    old_len = len(entries)

    if new_end_dt >= anchor_end_dt:
        entries[i]["end"] = new_end_str
        entries[i]["is_running"] = was_running if new_end_dt == anchor_end_dt else False
        del entries[i + 1:]
        clear_row_widget_keys(i + 1, old_len)
        save_state()
        return

    entries[i]["end"] = new_end_str
    entries[i]["is_running"] = False
    entries[i + 1:] = rebuild_chain(new_end_dt, anchor_end_dt, was_running)
    clear_row_widget_keys(i + 1, max(old_len, len(entries)))
    save_state()


def on_start_change(i):
    st.session_state.pop("edit_error", None)
    new_val = st.session_state[f"start_{i}"].strftime("%H:%M")
    apply_start_edit(i, new_val)


def on_end_change(i):
    st.session_state.pop("edit_error", None)
    apply_end_edit(i, st.session_state[f"end_{i}"])


def end_time_options(start_str, current_end):
    """Valid end times for an entry: 15-minute steps from start+15min up to the
    3-hour KaryaKeeper limit (clamped to the same day)."""
    start_dt = datetime.strptime(start_str, "%H:%M")
    limit = min(start_dt + timedelta(hours=3), start_dt.replace(hour=23, minute=45))
    options = []
    opt = start_dt + timedelta(minutes=15)
    while opt <= limit:
        options.append(opt.strftime("%H:%M"))
        opt += timedelta(minutes=15)
    if current_end not in options:
        options = sorted(set(options + [current_end]))
    return options


def on_project_change(i):
    entries = st.session_state["entries"]
    sel = st.session_state[f"project_{i}"]
    if sel == "-- Select project --":
        entries[i]["project_id"] = None
        entries[i]["project_text"] = ""
    else:
        proj = next(p for p in st.session_state["projects"] if p["text"] == sel)
        entries[i]["project_id"] = proj["value"]
        entries[i]["project_text"] = proj["text"]
    entries[i]["task_id"] = None
    entries[i]["task_title"] = ""
    st.session_state.pop(f"task_{i}", None)
    save_state()


def on_task_change(i, tasks):
    entries = st.session_state["entries"]
    sel = st.session_state[f"task_{i}"]
    if sel == "-- Select task --":
        entries[i]["task_id"] = None
        entries[i]["task_title"] = ""
    else:
        task = next(t for t in tasks if format_task(t) == sel)
        entries[i]["task_id"] = task["id"]
        entries[i]["task_title"] = task["title"]
    save_state()


def on_remark_change(i):
    entries = st.session_state["entries"]
    entries[i]["remark"] = st.session_state[f"remark_{i}"]
    save_state()


def format_task(task):
    group = task.get("group_name", "")
    title = task.get("title", "")
    return f"[{group}] {title}" if group else title


# ---------------------------------------------------------------- page layout

st.title("🗓️ KaryaKeeper Timesheet Automation")
st.caption("Fetches your GreytHR attendance, shows what is already logged in KaryaKeeper, and lets you log the remaining time — one entry at a time.")

with st.container(border=True):
    ctrl = st.columns([2.4, 1.8, 1.5, 4.3], vertical_alignment="bottom")
    with ctrl[0]:
        date_input = st.date_input("Date to log", value=datetime.now(ZoneInfo("Asia/Kolkata")).date())
    picked_str = date_input.strftime("%Y-%m-%d")
    # Each date keeps its own saved state; switching the picker swaps it in (or starts blank)
    if st.session_state.get("picker_date") != picked_str:
        st.session_state["picker_date"] = picked_str
        switch_to_date(picked_str)
    with ctrl[1]:
        fetch_clicked = st.button("🔍 Fetch Attendance", type="primary", use_container_width=True)
    with ctrl[2]:
        if st.session_state.get("target_date"):
            if st.button("🔄 Start Over", use_container_width=True):
                reset_state()
                st.rerun()
    if fetch_clicked:
        do_fetch_attendance(picked_str)

target_date = st.session_state.get("target_date")

if not target_date:
    nice_picked = datetime.strptime(picked_str, "%Y-%m-%d").strftime("%A, %d %B %Y")
    st.info(f"👆 No timesheet data loaded for **{nice_picked}** yet. Click **Fetch Attendance** to load it.")
    st.stop()

nice_date = datetime.strptime(target_date, "%Y-%m-%d").strftime("%A, %d %B %Y")

# ------------------------------------------------ 1. GreytHR attendance
st.subheader(f"📋 Attendance — {nice_date}")

sessions = st.session_state.get("sessions") or []
if sessions:
    lines = [
        "| # | In-Time | Out-Time | Duration | Break before |",
        "|:-:|:-:|:-:|:-:|:-:|",
    ]
    total_min = 0
    ongoing = False
    for idx, s in enumerate(sessions):
        out = s["out"] or "⏳ Not yet out"
        dur = fmt_minutes(s["minutes"]) if s["minutes"] is not None else "ongoing"
        brk = fmt_minutes(s["break_min"]) if s["break_min"] else "—"
        lines.append(f"| {idx + 1} | {s['in']} | {out} | {dur} | {brk} |")
        if s["minutes"] is not None:
            total_min += s["minutes"]
        else:
            ongoing = True
    st.markdown("\n".join(lines))
    total_label = f"Total worked: **{fmt_minutes(total_min)}**" if total_min else "Total worked: —"
    if ongoing:
        total_label += " *(plus an ongoing session)*"
    st.markdown(total_label)
else:
    st.caption("No swipe details available.")

# ------------------------------------------------ 2. Already logged in KaryaKeeper
st.subheader("🕑 Already Logged in KaryaKeeper")

existing_entries = st.session_state.get("existing_entries") or []
if existing_entries:
    st.caption("These entries are already saved in KaryaKeeper and are shown here for reference only.")
    logged_rows = [
        {
            "Start": e["start"],
            "End": e["end"],
            "Duration": fmt_minutes(
                (datetime.strptime(e["end"], "%H:%M") - datetime.strptime(e["start"], "%H:%M")).total_seconds() // 60
            ),
            "Project": e["project"],
            "Task": e["task"],
            "Remark": e["remark"],
        }
        for e in sorted(existing_entries, key=lambda e: (e["start"], e["end"]))
    ]
    st.dataframe(logged_rows, hide_index=True, use_container_width=True)
else:
    st.caption("No entries logged yet for this date.")

st.divider()

# ------------------------------------------------ 3. New entries to log
entries = st.session_state.get("entries", [])

st.subheader("✍️ New Entries to Log")

if not entries:
    st.success(f"🎉 All attendance for **{nice_date}** has already been logged in KaryaKeeper. Nothing to do.")
    st.stop()

saved_count = sum(1 for e in entries if e["saved"])
st.progress(saved_count / len(entries), text=f"{saved_count} of {len(entries)} entries logged")
st.caption("Times are rounded to 15-minute steps and split into blocks of at most 3 hours. "
           "Changing a time rebuilds the rows below it automatically. Each **Save** logs that one entry to KaryaKeeper immediately.")

if st.session_state.get("edit_error"):
    st.error(st.session_state.pop("edit_error"))

for i, entry in enumerate(entries):
    saved = entry["saved"]
    locked = saved or row_is_locked_by_later_save(i)
    duration_min = (datetime.strptime(entry["end"], "%H:%M") - datetime.strptime(entry["start"], "%H:%M")).total_seconds() // 60

    with st.container(border=True):
        head = st.columns([8.5, 1.5], vertical_alignment="center")
        with head[0]:
            title = f"**Entry {i + 1}** &nbsp;·&nbsp; {entry['start']} – {entry['end']} &nbsp;·&nbsp; {fmt_minutes(duration_min)}"
            if entry["is_running"] and not saved:
                title += " &nbsp;·&nbsp; :orange[⏳ ongoing]"
            st.markdown(title)
        with head[1]:
            st.markdown(":green[✅ Saved]" if saved else ":orange[● Pending]")

        if saved:
            st.caption(f"{entry['project_text']} → {entry['task_title']} — “{entry['remark']}”")
            continue

        if entry["is_running"]:
            st.warning("You are still clocked in — the end time of this entry is based on the current time.", icon="⚠️")

        widgets = st.columns([1.2, 1.2, 2.3, 2.9], vertical_alignment="top")
        with widgets[0]:
            st.time_input(
                "Start time", value=datetime.strptime(entry["start"], "%H:%M").time(),
                key=f"start_{i}", disabled=locked,
                on_change=on_start_change, args=(i,),
                help="Locked while a later entry is already saved." if locked else None,
            )
        with widgets[1]:
            options = end_time_options(entry["start"], entry["end"])
            st.selectbox(
                "End time", options, index=options.index(entry["end"]),
                key=f"end_{i}", disabled=locked,
                on_change=on_end_change, args=(i,),
                help="Locked while a later entry is already saved." if locked
                     else "Limited to 3 hours after the start time (KaryaKeeper's per-entry limit).",
            )
        with widgets[2]:
            options = ["-- Select project --"] + [p["text"] for p in st.session_state["projects"]]
            index = options.index(entry["project_text"]) if entry["project_text"] in options else 0
            st.selectbox(
                "Project", options, index=index, key=f"project_{i}",
                on_change=on_project_change, args=(i,),
            )
        with widgets[3]:
            if not entry["project_id"]:
                st.selectbox("Task", ["-- Select a project first --"], disabled=True, key=f"task_placeholder_{i}")
            else:
                tasks = get_tasks_for_project(entry["project_id"])
                if not tasks:
                    st.selectbox("Task", ["-- No tasks found --"], disabled=True, key=f"task_placeholder_{i}")
                else:
                    options = ["-- Select task --"] + [format_task(t) for t in tasks]
                    current_label = next((format_task(t) for t in tasks if t["id"] == entry["task_id"]), None)
                    index = options.index(current_label) if current_label in options else 0
                    st.selectbox(
                        "Task", options, index=index, key=f"task_{i}",
                        on_change=on_task_change, args=(i, tasks),
                    )

        bottom = st.columns([8.4, 1.6], vertical_alignment="bottom")
        with bottom[0]:
            st.text_input(
                "Remark / description", value=entry["remark"], key=f"remark_{i}",
                placeholder="What did you work on during this block?",
                on_change=on_remark_change, args=(i,),
            )
        with bottom[1]:
            can_save = bool(entry["project_id"] and entry["task_id"] and entry["remark"].strip())
            if st.button(
                "💾 Save", key=f"save_{i}", type="primary",
                disabled=not can_save, use_container_width=True,
                help=None if can_save else "Select a project and task, and add a remark first.",
            ):
                do_save_entry(i)
                st.rerun()

if saved_count == len(entries):
    st.success("🎉 All entries have been logged to KaryaKeeper. You're done for the day!")
