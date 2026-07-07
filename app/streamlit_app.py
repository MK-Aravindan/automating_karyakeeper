import os
import sys
import json
import atexit
import asyncio
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Streamlit forces the Windows selector event loop, which cannot spawn subprocesses;
# Playwright needs the proactor loop to launch its browser driver.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import streamlit as st
from dotenv import load_dotenv

import karyakeeper_core as kkc
from browser_worker import BrowserWorker

ROOT_DIR = kkc.ROOT_DIR
DATA_DIR = kkc.DATA_DIR
STATE_FILE = kkc.STATE_FILE
DATE_KEYS = ("entries", "projects", "tasks_cache", "sessions", "existing_entries")
STORE_LOCK = threading.RLock()

st.set_page_config(page_title="KaryaKeeper Automation", page_icon="🗓️", layout="wide")

st.markdown("""
<style>
.block-container {max-width: 1250px; padding-top: 2.2rem; margin: auto;}
.stButton button {border-radius: 8px;}
.stButton button p {white-space: nowrap;}
h1 {letter-spacing: -0.3px;}
.kk-summary {display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:1rem; margin:0.75rem 0 1.25rem;}
.kk-summary > div {min-width:0;}
/* Inherit theme colors so the summary stays readable in dark mode too */
.kk-summary dt {font-size:0.875rem; opacity:0.65; margin-bottom:0.15rem;}
.kk-summary dd {font-size:2rem; line-height:1.2; margin:0; font-weight:600;}
/* Pin the attendance column so it stays visible while the entries list scrolls */
[data-testid="stColumn"]:has(.attendance-sticky) {position: sticky; top: 3.5rem; align-self: flex-start;}
/* The marker is only a positioning hook; keep it in the DOM for :has() but give it
   no layout box so it adds no empty gap above the attendance heading */
[data-testid="stElementContainer"]:has(.attendance-sticky) {display: none;}
/* Keep the narrow attendance table readable and scrollable instead of clipped */
[data-testid="stColumn"]:has(.attendance-sticky) table {font-size: 0.85rem;}
[data-testid="stColumn"]:has(.attendance-sticky) .stMarkdown {overflow-x: auto;}
/* Below the width where a side panel would cramp, stack both panels full-width so
   everything stays fully visible (attendance first, then the entries to fill in) */
@media (max-width: 1150px) {
  [data-testid="stHorizontalBlock"]:has(.attendance-sticky) {flex-direction: column;}
  [data-testid="stHorizontalBlock"]:has(.attendance-sticky) > [data-testid="stColumn"] {width: 100% !important; flex: 1 1 100% !important;}
  [data-testid="stColumn"]:has(.attendance-sticky) {position: static; order: -1;}
}
@media (max-width: 700px) {
  .block-container {padding: 1rem 0.75rem 2rem;}
  h1 {font-size: 2rem !important; line-height: 1.15 !important;}
  [data-testid="stHorizontalBlock"] {gap: 0.65rem;}
  .stButton button {min-height: 44px;}
  .kk-summary {grid-template-columns:repeat(2,minmax(0,1fr)); gap:1rem 0.75rem;}
  .kk-summary dd {font-size:1.65rem;}
  /* On stacked mobile layout the panel should scroll normally, not pin */
  [data-testid="stColumn"]:has(.attendance-sticky) {position: static;}
}
</style>
""", unsafe_allow_html=True)

migrated_local_data = kkc.ensure_local_storage()
# utf-8-sig so a .env saved by Notepad with a BOM still parses correctly
load_dotenv(kkc.CONFIG_FILE, encoding="utf-8-sig")
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
    st.error(
        f"Missing credentials in the local configuration: {', '.join(missing)}. "
        f"Run setup.bat and complete {kkc.CONFIG_FILE}."
    )
    st.stop()


@st.cache_resource(show_spinner=False)
def get_worker():
    """One browser worker for the whole app run. Its login sessions live only in
    memory, so nothing needs cleaning up beyond closing the browser on exit."""
    w = BrowserWorker(GT_DOMAIN, GT_USER, GT_PASS, KK_URL, KK_USER, KK_PASS)
    atexit.register(w.close)
    return w


def load_store():
    """Returns the full {date: state} map. Migrates older state files if found."""
    with STORE_LOCK:
        data, recovered = kkc.load_json_with_backup(STATE_FILE)
    if recovered:
        st.session_state["store_warning"] = "Saved progress was recovered from the last valid backup."
    if not isinstance(data, dict):
        raise kkc.StateStoreError("Saved progress has an invalid structure and was not overwritten.")
    if "entries" in data:  # old single-date format, keyed by target_date inside
        td = data.get("target_date")
        return {td: {k: data.get(k) for k in DATE_KEYS}} if td else {}
    return data


def save_store(store):
    if not isinstance(store, dict):
        raise kkc.StateStoreError("Refusing to save invalid progress data.")
    with STORE_LOCK:
        kkc.atomic_write_json(STATE_FILE, store)


def save_state():
    """Persist the active date's working data into the per-date store."""
    td = st.session_state.get("target_date")
    if not td:
        return
    with STORE_LOCK:
        store = load_store()
        store[td] = {k: st.session_state.get(k) for k in DATE_KEYS}
        save_store(store)


def clear_all_row_widget_keys():
    for k in list(st.session_state.keys()):
        if k.startswith((
            "start_", "end_", "project_", "task_", "remark_", "task_placeholder_",
            "task_loading_", "task_error_", "save_", "apply_below_", "skip_", "restore_",
        )):
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


def new_entry(start, end, is_running=False, chain_id=0, source_start=None, source_end=None, manual=False):
    return {
        "start": start,
        "end": end,
        "is_running": is_running,
        "chain_id": chain_id,
        "source_start": source_start or start,
        "source_end": source_end or end,
        "manual": manual,
        "project_id": None,
        "project_text": "",
        "task_id": None,
        "task_title": "",
        "remark": "",
        "saved": False,
        "skipped": False,
    }


def ensure_entry_metadata(entries):
    """Upgrade saved rows from older versions without discarding user input."""
    chain_id = 0
    chain_start_idx = 0
    for i, entry in enumerate(entries):
        if i and entries[i - 1].get("end") != entry.get("start"):
            previous_end = entries[i - 1].get("end")
            for row in entries[chain_start_idx:i]:
                row.setdefault("source_end", previous_end)
            chain_id += 1
            chain_start_idx = i
        entry.setdefault("chain_id", chain_id)
        entry.setdefault("source_start", entries[chain_start_idx].get("start", entry.get("start")))
        entry.setdefault("manual", False)
        entry.setdefault("skipped", False)
    if entries:
        final_end = entries[-1].get("end")
        for row in entries[chain_start_idx:]:
            row.setdefault("source_end", final_end)
    return entries


def switch_to_date(date_str):
    """Load a date's saved working data into the active session, or start blank if
    that date has not been fetched yet."""
    clear_all_row_widget_keys()
    saved = load_store().get(date_str)
    if not saved:
        blank_working_state()
        return
    st.session_state["entries"] = ensure_entry_metadata(saved.get("entries") or [])
    st.session_state["projects"] = saved.get("projects") or []
    # Drop failed/empty cached task lists so they are fetched again
    st.session_state["tasks_cache"] = {k: v for k, v in (saved.get("tasks_cache") or {}).items() if v}
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


def fmt_minutes(m):
    h, mm = divmod(int(m), 60)
    if h and mm:
        return f"{h} hr {mm} min"
    if h:
        return f"{h} hr"
    return f"{mm} min"


def build_session_rows(swipes):
    rows = []
    prev_out = None
    now_ist = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
    for in_dt, out_dt in kkc.swipe_sessions(swipes):
        rows.append({
            "in": in_dt.strftime("%H:%M"),
            "out": out_dt.strftime("%H:%M") if out_dt else None,
            "minutes": int((out_dt - in_dt).total_seconds() // 60) if out_dt else None,
            "elapsed": None if out_dt else max(0, int((now_ist - in_dt).total_seconds() // 60)),
            "break_min": int((in_dt - prev_out).total_seconds() // 60) if prev_out else None,
        })
        if out_dt:
            prev_out = out_dt
    return rows


def do_fetch_attendance(target_date):
    started = time.perf_counter()
    status = st.status("Fetching your timesheet data...", expanded=True)
    try:
        status.write("Preparing the secure browser session...")
        worker = get_worker()
        if worker.gt_session_active:
            status.write("**Step 1/4** — Reading your GreytHR swipes (session already active)...")
        else:
            status.write("**Step 1/4** — Logging into GreytHR and reading your swipes — the first fetch after starting the app is the slowest step...")
        swipes = worker.fetch_swipes(target_date)
        if not swipes:
            status.update(label="No attendance punches found for this date.", state="error")
            return

        blocks = kkc.consolidate_blocks(swipes)
        if not blocks:
            status.update(label="Could not build any work blocks from the swipe data.", state="error")
            return
        incomplete = next(((start, end) for start, end in blocks if end is None), None)
        if incomplete and target_date != datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d"):
            raise kkc.IncompleteHistoricalAttendanceError(
                f"Attendance for {target_date} has an IN punch at {incomplete[0].strftime('%H:%M')} "
                "without a matching OUT punch. Correct it in GreytHR or add a manual entry."
            )

        status.write(
            "**Step 2/4** — Reading KaryaKeeper entries and projects (reusing the active session)..."
            if worker.kk_session_active
            else "**Step 2/4** — Logging into KaryaKeeper and loading your workspace..."
        )
        existing_entries, projects = worker.fetch_karyakeeper_context(target_date)
        existing_times = [(e["start"], e["end"]) for e in existing_entries]

        status.write("**Step 3/4** — Reconciling attendance with time already logged...")
        filtered_blocks = kkc.filter_existing_blocks(blocks, existing_times, target_date)
        status.write("**Step 4/4** — Preparing safe entry blocks of at most 3 hours...")
        if not projects and filtered_blocks:
            status.update(label="No projects found in your KaryaKeeper account.", state="error")
            return False

        entries = []
        for chain_id, (block_start, block_end, is_running) in enumerate(filtered_blocks):
            for start_dt, end_dt, chunk_running in kkc.process_and_chunk_blocks([
                (block_start, block_end, is_running)
            ]):
                entries.append(new_entry(
                    start_dt.strftime("%H:%M"),
                    end_dt.strftime("%H:%M"),
                    is_running=chunk_running,
                    chain_id=chain_id,
                    source_start=block_start.strftime("%H:%M"),
                    source_end=block_end.strftime("%H:%M"),
                ))

        st.session_state["target_date"] = target_date
        st.session_state["entries"] = entries
        st.session_state["projects"] = projects
        st.session_state["tasks_cache"] = {}
        st.session_state["sessions"] = build_session_rows(swipes)
        st.session_state["existing_entries"] = existing_entries
        clear_all_row_widget_keys()
        st.session_state["last_fetch_seconds"] = round(time.perf_counter() - started, 2)
        save_state()

        if entries:
            status.update(
                label=(
                    f"Done in {st.session_state['last_fetch_seconds']:.1f}s — "
                    f"found {len(entries)} unlogged time block(s) for {target_date}."
                ),
                state="complete",
                expanded=False,
            )
        else:
            status.update(
                label=f"Done in {st.session_state['last_fetch_seconds']:.1f}s — all time is already logged.",
                state="complete",
                expanded=False,
            )
        return True
    except kkc.IncompleteHistoricalAttendanceError as e:
        status.update(label=str(e), state="error")
        return False
    except Exception as e:
        status.update(label=f"Unexpected error: {e}", state="error")
        return False


def validate_entry(i):
    entries = st.session_state["entries"]
    entry = entries[i]
    target_date = st.session_state["target_date"]
    try:
        start_dt, end_dt = kkc.parse_clock_interval(target_date, entry["start"], entry["end"])
    except (TypeError, ValueError):
        return "Enter a valid start and end time."
    duration = int((end_dt - start_dt).total_seconds() // 60)
    if duration <= 0:
        return "End time must be after start time."
    if duration > 180:
        return "Entries cannot exceed KaryaKeeper's 3-hour limit."

    intervals = []
    for existing in st.session_state.get("existing_entries") or []:
        intervals.append((
            f"an already logged entry ({existing['start']}–{existing['end']})",
            existing["start"],
            existing["end"],
        ))
    for j, other in enumerate(entries):
        if j == i or other.get("skipped"):
            continue
        intervals.append((f"Entry {j + 1} ({other['start']}–{other['end']})", other["start"], other["end"]))
    conflict = kkc.find_interval_conflict(target_date, entry["start"], entry["end"], intervals)
    return f"This time overlaps {conflict}." if conflict else None


def do_save_entry(i, show_feedback=True):
    entry = st.session_state["entries"][i]
    kk_date = datetime.strptime(st.session_state["target_date"], "%Y-%m-%d").strftime("%d/%m/%Y")

    validation_error = validate_entry(i)
    if validation_error:
        st.error(f"Entry {i + 1}: {validation_error}")
        return False

    try:
        worker = get_worker()
        result = worker.save_entry(
            kk_date, entry["start"], entry["end"], entry["remark"],
            entry["project_id"], entry["task_id"], entry["task_title"],
        )
    except Exception as e:
        st.error(f"Failed to save entry {i + 1}: {e}")
        return False

    entry["saved"] = True
    saved_record = {
        "project": entry["project_text"],
        "task": entry["task_title"],
        "remark": entry["remark"],
        "start": entry["start"],
        "end": entry["end"],
    }
    existing = st.session_state.get("existing_entries") or []
    if not any(
        e.get("start") == saved_record["start"]
        and e.get("end") == saved_record["end"]
        and e.get("remark") == saved_record["remark"]
        for e in existing
    ):
        st.session_state["existing_entries"] = existing + [saved_record]
    save_state()
    if show_feedback:
        suffix = " (verified after a slow confirmation)" if result.get("status") == "verified_after_timeout" else ""
        st.toast(f"Entry {i + 1} logged to KaryaKeeper{suffix}.", icon="✅")
    return True


def rebuild_chain(start_dt, end_dt, preserve_running, chain_id, source_start, source_end):
    """Re-chunk (start_dt, end_dt) into <=3h pieces, the same rule used for the initial fetch."""
    chunked = kkc.process_and_chunk_blocks([(start_dt, end_dt, preserve_running)])
    return [
        new_entry(
            s.strftime("%H:%M"),
            e.strftime("%H:%M"),
            is_running=r,
            chain_id=chain_id,
            source_start=source_start,
            source_end=source_end,
        )
        for s, e, r in chunked
    ]


def chain_bounds(i):
    entries = st.session_state["entries"]
    chain_id = entries[i].get("chain_id")
    start = i
    end = i
    while start > 0 and entries[start - 1].get("chain_id") == chain_id:
        start -= 1
    while end + 1 < len(entries) and entries[end + 1].get("chain_id") == chain_id:
        end += 1
    return start, end


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
    entry = entries[i]
    new_start_dt = datetime.strptime(f"{target_date} {new_start_str}", "%Y-%m-%d %H:%M")

    if entry.get("manual"):
        current_end = datetime.strptime(f"{target_date} {entry['end']}", "%Y-%m-%d %H:%M")
        if new_start_dt >= current_end:
            st.session_state["edit_error"] = "Start time must be before this manual entry's end time."
            return False
        entry["start"] = new_start_str
        entry["source_start"] = new_start_str
        save_state()
        return True

    source_start_dt = datetime.strptime(f"{target_date} {entry['source_start']}", "%Y-%m-%d %H:%M")
    anchor_end_dt = datetime.strptime(f"{target_date} {entry['source_end']}", "%Y-%m-%d %H:%M")

    if new_start_dt < source_start_dt or new_start_dt >= anchor_end_dt:
        st.session_state["edit_error"] = (
            f"Start time must stay within this attendance block "
            f"({entry['source_start']}–{entry['source_end']})."
        )
        return False
    chain_start, chain_end = chain_bounds(i)
    if i > chain_start:
        previous_end = datetime.strptime(f"{target_date} {entries[i - 1]['end']}", "%Y-%m-%d %H:%M")
        if new_start_dt < previous_end:
            st.session_state["edit_error"] = f"Start time overlaps Entry {i}."
            return False

    was_running = entries[chain_end]["is_running"]
    old_len = len(entries)
    chain = rebuild_chain(
        new_start_dt, anchor_end_dt, was_running, entry["chain_id"],
        entry["source_start"], entry["source_end"],
    )
    for field in ("project_id", "project_text", "task_id", "task_title", "remark"):
        chain[0][field] = entry.get(field)
    entries[i:chain_end + 1] = chain

    clear_row_widget_keys(i, max(old_len, len(entries)))
    save_state()
    return True


def apply_end_edit(i, new_end_str):
    entries = st.session_state["entries"]
    target_date = st.session_state["target_date"]
    entry = entries[i]
    row_start_dt = datetime.strptime(f"{target_date} {entries[i]['start']}", "%Y-%m-%d %H:%M")
    new_end_dt = datetime.strptime(f"{target_date} {new_end_str}", "%Y-%m-%d %H:%M")

    if new_end_dt <= row_start_dt:
        st.session_state["edit_error"] = "End time must be after this row's start time."
        return False

    if new_end_dt - row_start_dt > timedelta(hours=3):
        st.session_state["edit_error"] = "An entry cannot be longer than 3 hours."
        return False

    if entry.get("manual"):
        entries[i]["end"] = new_end_str
        entries[i]["source_end"] = new_end_str
        entries[i]["is_running"] = False
        save_state()
        return True

    chain_start, chain_end = chain_bounds(i)
    anchor_end_dt = datetime.strptime(f"{target_date} {entry['source_end']}", "%Y-%m-%d %H:%M")
    if new_end_dt > anchor_end_dt:
        st.session_state["edit_error"] = (
            f"End time cannot exceed this attendance block's {entry['source_end']} boundary."
        )
        return False
    was_running = entries[chain_end]["is_running"]
    old_len = len(entries)

    if new_end_dt >= anchor_end_dt:
        entries[i]["end"] = new_end_str
        entries[i]["is_running"] = was_running if new_end_dt == anchor_end_dt else False
        del entries[i + 1:chain_end + 1]
        clear_row_widget_keys(i + 1, old_len)
        save_state()
        return True

    entries[i]["end"] = new_end_str
    entries[i]["is_running"] = False
    entries[i + 1:chain_end + 1] = rebuild_chain(
        new_end_dt, anchor_end_dt, was_running, entry["chain_id"],
        entry["source_start"], entry["source_end"],
    )
    clear_row_widget_keys(i + 1, max(old_len, len(entries)))
    save_state()
    return True


def on_start_change(i):
    st.session_state.pop("edit_error", None)
    if f"start_{i}" not in st.session_state:
        return
    new_val = st.session_state[f"start_{i}"].strftime("%H:%M")
    if not apply_start_edit(i, new_val):
        st.session_state[f"start_{i}"] = datetime.strptime(
            st.session_state["entries"][i]["start"], "%H:%M"
        ).time()


def on_end_change(i):
    st.session_state.pop("edit_error", None)
    if f"end_{i}" not in st.session_state:
        return
    if not apply_end_edit(i, st.session_state[f"end_{i}"]):
        st.session_state[f"end_{i}"] = st.session_state["entries"][i]["end"]


def end_time_options(start_str, current_end, maximum_end=None):
    """Valid end times for an entry: 15-minute steps from start+15min up to the
    3-hour KaryaKeeper limit (clamped to the same day)."""
    start_dt = datetime.strptime(start_str, "%H:%M")
    limit = min(start_dt + timedelta(hours=3), start_dt.replace(hour=23, minute=45))
    if maximum_end:
        limit = min(limit, datetime.strptime(maximum_end, "%H:%M"))
    options = []
    opt = start_dt + timedelta(minutes=15)
    while opt <= limit:
        options.append(opt.strftime("%H:%M"))
        opt += timedelta(minutes=15)
    if current_end not in options:
        options = sorted(set(options + [current_end]))
    return options


def on_project_change(i):
    if f"project_{i}" not in st.session_state:
        return
    entries = st.session_state["entries"]
    selected_id = st.session_state[f"project_{i}"]
    if not selected_id:
        entries[i]["project_id"] = None
        entries[i]["project_text"] = ""
    else:
        proj = next(p for p in st.session_state["projects"] if str(p["value"]) == str(selected_id))
        entries[i]["project_id"] = str(proj["value"])
        entries[i]["project_text"] = proj["text"]
        # A previous failed load is marked with None; picking the project again retries it
        project_key = str(proj["value"])
        if st.session_state["tasks_cache"].get(project_key, "") is None:
            st.session_state["tasks_cache"].pop(project_key)
    entries[i]["task_id"] = None
    entries[i]["task_title"] = ""
    st.session_state.pop(f"task_{i}", None)
    save_state()


def on_task_change(i, tasks):
    if f"task_{i}" not in st.session_state:
        return
    entries = st.session_state["entries"]
    selected_id = st.session_state[f"task_{i}"]
    if not selected_id:
        entries[i]["task_id"] = None
        entries[i]["task_title"] = ""
    else:
        task = next(t for t in tasks if str(t["id"]) == str(selected_id))
        entries[i]["task_id"] = str(task["id"])
        entries[i]["task_title"] = task["title"]
    save_state()


def on_remark_change(i):
    if f"remark_{i}" not in st.session_state:
        return
    entries = st.session_state["entries"]
    entries[i]["remark"] = st.session_state[f"remark_{i}"]
    save_state()


def format_task(task):
    group = task.get("group_name", "")
    title = task.get("title", "")
    return f"[{group}] {title}" if group else title


def copy_details_to_below(i):
    source = st.session_state["entries"][i]
    for idx, entry in enumerate(st.session_state["entries"][i + 1:], start=i + 1):
        if entry.get("saved") or entry.get("skipped"):
            continue
        for field in ("project_id", "project_text", "task_id", "task_title", "remark"):
            entry[field] = source.get(field)
        for prefix in ("project_", "task_", "remark_"):
            st.session_state.pop(f"{prefix}{idx}", None)
    save_state()


def toggle_skip(i):
    entry = st.session_state["entries"][i]
    entry["skipped"] = not entry.get("skipped", False)
    save_state()


def add_manual_entry():
    entries = st.session_state["entries"]
    if entries:
        start = entries[-1]["end"]
    else:
        start = "09:00"
    start_dt = datetime.strptime(start, "%H:%M")
    end = min(start_dt + timedelta(minutes=60), start_dt.replace(hour=23, minute=45)).strftime("%H:%M")
    if end <= start:
        start, end = "09:00", "10:00"
    next_chain = max((int(e.get("chain_id", 0)) for e in entries), default=-1) + 1
    entries.append(new_entry(start, end, chain_id=next_chain, manual=True))
    clear_all_row_widget_keys()
    save_state()


def entry_is_ready(i):
    entry = st.session_state["entries"][i]
    return bool(
        not entry.get("saved")
        and not entry.get("skipped")
        and entry.get("project_id")
        and entry.get("task_id")
        and entry.get("remark", "").strip()
        and not validate_entry(i)
    )


def duration_minutes(start, end):
    start_dt, end_dt = kkc.parse_clock_interval(st.session_state["target_date"], start, end)
    return int((end_dt - start_dt).total_seconds() // 60)


def bulk_save_ready_entries(indices):
    status = st.status(f"Saving {len(indices)} ready entries...", expanded=True)
    saved = 0
    for position, index in enumerate(indices, start=1):
        status.write(f"Saving Entry {index + 1} ({position}/{len(indices)})...")
        if not do_save_entry(index, show_feedback=False):
            status.update(
                label=f"Stopped after {saved} successful save(s). Review Entry {index + 1} before continuing.",
                state="error",
            )
            return False
        saved += 1
    status.update(label=f"Saved {saved} entries successfully.", state="complete", expanded=False)
    st.toast(f"{saved} entries logged to KaryaKeeper.", icon="✅")
    return True


# ---------------------------------------------------------------- page layout

st.title("🗓️ Log your KaryaKeeper timesheet")
st.caption("Review attendance, fill the remaining blocks, and save safely without re-entering the same details.")

if migrated_local_data:
    st.success(
        f"Moved {', '.join(migrated_local_data)} to your private local application folder.",
        icon="🔒",
    )
if st.session_state.get("store_warning"):
    st.warning(st.session_state.pop("store_warning"), icon="⚠️")

today = datetime.now(ZoneInfo("Asia/Kolkata")).date()

with st.container(border=True):
    ctrl = st.columns([2.4, 2.0, 1.8, 3.8], vertical_alignment="bottom")
    with ctrl[0]:
        date_input = st.date_input("Date to log", value=today, max_value=today)
    picked_str = date_input.strftime("%Y-%m-%d")
    if st.session_state.get("picker_date") != picked_str:
        st.session_state["picker_date"] = picked_str
        switch_to_date(picked_str)

    has_loaded_date = st.session_state.get("target_date") == picked_str
    with ctrl[1]:
        fetch_clicked = st.button(
            "🔄 Refresh attendance" if has_loaded_date else "🔍 Fetch attendance",
            type="primary",
            use_container_width=True,
        )
    with ctrl[2]:
        clear_clicked = st.button(
            "Clear local draft",
            disabled=not has_loaded_date,
            use_container_width=True,
        )

    st.caption(
        "🔒 Credentials and progress are stored in your private local application folder. "
        "Browser sign-in sessions remain in memory only."
    )

    if fetch_clicked:
        if has_loaded_date and st.session_state.get("entries"):
            st.session_state["confirm_refresh"] = True
        else:
            do_fetch_attendance(picked_str)
    if clear_clicked:
        st.session_state["confirm_clear"] = True

    if st.session_state.get("confirm_refresh"):
        st.warning("Refreshing replaces the current unsaved rows after the new data loads successfully.")
        confirm_cols = st.columns([2, 2, 6])
        refresh_now = False
        with confirm_cols[0]:
            if st.button("Refresh now", type="primary", use_container_width=True):
                st.session_state.pop("confirm_refresh", None)
                refresh_now = True
        with confirm_cols[1]:
            if st.button("Cancel", key="cancel_refresh", use_container_width=True):
                st.session_state.pop("confirm_refresh", None)
                st.rerun()
        # Run the fetch at container level so its status panel is not squeezed
        # into the narrow confirmation column
        if refresh_now:
            do_fetch_attendance(picked_str)

    if st.session_state.get("confirm_clear"):
        st.warning("Clear only this date's local draft? Entries already saved in KaryaKeeper are not deleted.")
        clear_cols = st.columns([2, 2, 6])
        with clear_cols[0]:
            if st.button("Clear this date", type="primary", use_container_width=True):
                st.session_state.pop("confirm_clear", None)
                reset_state()
                st.rerun()
        with clear_cols[1]:
            if st.button("Keep draft", use_container_width=True):
                st.session_state.pop("confirm_clear", None)
                st.rerun()

target_date = st.session_state.get("target_date")

if not target_date:
    nice_picked = datetime.strptime(picked_str, "%Y-%m-%d").strftime("%A, %d %B %Y")
    st.info(f"No timesheet data loaded for **{nice_picked}**. Select **Fetch attendance** to begin.")
    st.stop()

nice_date = datetime.strptime(target_date, "%Y-%m-%d").strftime("%A, %d %B %Y")
sessions = st.session_state.get("sessions") or []
existing_entries = st.session_state.get("existing_entries") or []
entries = st.session_state.get("entries", [])

worked_minutes = sum(s["minutes"] if s["minutes"] is not None else (s.get("elapsed") or 0) for s in sessions)
logged_minutes = kkc.total_interval_minutes(
    target_date,
    [(entry["start"], entry["end"]) for entry in existing_entries],
)
remaining_minutes = kkc.total_interval_minutes(
    target_date,
    [
        (entry["start"], entry["end"])
        for entry in entries
        if not entry.get("saved") and not entry.get("skipped")
    ],
)
processed_count = sum(bool(e.get("saved") or e.get("skipped")) for e in entries)

st.markdown(
    f"""
    <dl class="kk-summary">
      <div><dt>Attendance</dt><dd>{fmt_minutes(worked_minutes) if worked_minutes else '—'}</dd></div>
      <div><dt>Already logged</dt><dd>{fmt_minutes(logged_minutes) if logged_minutes else '—'}</dd></div>
      <div><dt>Remaining to log in KaryaKeeper</dt><dd>{fmt_minutes(remaining_minutes) if remaining_minutes else '—'}</dd></div>
      <div><dt>Completed</dt><dd>{f'{processed_count}/{len(entries)}' if entries else 'Done'}</dd></div>
    </dl>
    """,
    unsafe_allow_html=True,
)

main_col, side_col = st.columns([2.4, 1], gap="large")

# Attendance is pinned in the side column (see the sticky CSS) so it stays in view
with side_col:
    st.markdown('<div class="attendance-sticky"></div>', unsafe_allow_html=True)
    st.subheader("📋 Attendance details")
    if sessions:
        lines = [
            "| # | In | Out | Duration | Break |",
            "|:-:|:-:|:-:|:-:|:-:|",
        ]
        ongoing = False
        for idx, session in enumerate(sessions):
            out = session["out"] or "Not yet out"
            if session["minutes"] is not None:
                duration = fmt_minutes(session["minutes"])
            elif session.get("elapsed"):
                duration = f"{fmt_minutes(session['elapsed'])} so far"
            else:
                duration = "ongoing"
            break_before = fmt_minutes(session["break_min"]) if session["break_min"] else "—"
            lines.append(f"| {idx + 1} | {session['in']} | {out} | {duration} | {break_before} |")
            ongoing = ongoing or session["minutes"] is None
        st.markdown("\n".join(lines))
        total_label = f"Total worked: **{fmt_minutes(worked_minutes)}**" if worked_minutes else "Total worked: —"
        if ongoing:
            total_label += " *(incl. ongoing session, as of last refresh)*"
        st.markdown(total_label)
    else:
        st.caption("No swipe details available.")

with main_col:
    st.subheader(f"🕑 Already logged in KaryaKeeper ({len(existing_entries)})")
    if existing_entries:
        st.caption("These entries are already saved in KaryaKeeper and are shown here for reference only.")
        logged_rows = [
            {
                "Start": entry["start"],
                "End": entry["end"],
                "Duration": fmt_minutes(duration_minutes(entry["start"], entry["end"])),
                "Project": entry["project"],
                "Task": entry["task"],
                "Remark": entry["remark"],
            }
            for entry in sorted(existing_entries, key=lambda item: (item["start"], item["end"]))
        ]
        st.dataframe(logged_rows, hide_index=True, use_container_width=True)
    else:
        st.caption("No entries are logged for this date.")

    st.divider()

    st.subheader(f"✍️ Entries to complete — {nice_date}")

    if not entries:
        st.success("All detected attendance is already logged. No new entry is required.", icon="✅")
    else:
        st.progress(processed_count / len(entries), text=f"{processed_count} of {len(entries)} entries completed")
        st.caption(
            "Times stay inside their attendance block and each entry is limited to 3 hours. "
            "Skipped rows remain local and are never submitted."
        )

        ready_indices = [i for i in range(len(entries)) if entry_is_ready(i)]
        actions = st.columns([2.1, 2.4, 5.5])
        with actions[0]:
            if st.button("＋ Add manual entry", use_container_width=True):
                add_manual_entry()
                st.rerun()
        with actions[1]:
            save_all_clicked = st.button(
                f"💾 Save all ready ({len(ready_indices)})",
                type="primary",
                disabled=not ready_indices,
                use_container_width=True,
            )
        if save_all_clicked and bulk_save_ready_entries(ready_indices):
            st.rerun()

        if st.session_state.get("edit_error"):
            st.error(st.session_state.pop("edit_error"))

        for i, entry in enumerate(entries):
            saved = entry.get("saved", False)
            skipped = entry.get("skipped", False)
            locked = saved or row_is_locked_by_later_save(i)
            entry_duration = duration_minutes(entry["start"], entry["end"])

            with st.container(border=True):
                head = st.columns([8.2, 1.8], vertical_alignment="center")
                with head[0]:
                    kind = "Manual" if entry.get("manual") else f"Entry {i + 1}"
                    title = f"**{kind}** &nbsp;·&nbsp; {entry['start']}–{entry['end']} &nbsp;·&nbsp; {fmt_minutes(entry_duration)}"
                    if entry.get("is_running") and not saved:
                        title += " &nbsp;·&nbsp; :orange[ongoing]"
                    st.markdown(title)
                with head[1]:
                    if saved:
                        st.markdown(":green[✅ Saved]")
                    elif skipped:
                        st.markdown(":gray[— Skipped]")
                    else:
                        st.markdown(":orange[● Pending]")

                if saved:
                    st.caption(f"{entry['project_text']} → {entry['task_title']} — “{entry['remark']}”")
                    continue
                if skipped:
                    restore_cols = st.columns([8, 2])
                    restore_cols[0].caption("This row will not be submitted to KaryaKeeper.")
                    with restore_cols[1]:
                        if st.button("Restore", key=f"restore_{i}", use_container_width=True):
                            toggle_skip(i)
                            st.rerun()
                    continue

                if entry.get("is_running"):
                    st.warning("You are still clocked in; refresh attendance after clocking out before the final save.", icon="⚠️")

                widgets = st.columns([1.6, 1.6, 2.2, 2.6], vertical_alignment="top")
                with widgets[0]:
                    st.time_input(
                        "Start time",
                        value=datetime.strptime(entry["start"], "%H:%M").time(),
                        key=f"start_{i}",
                        disabled=locked,
                        step=900,
                        on_change=on_start_change,
                        args=(i,),
                        help="Locked while a later entry is saved." if locked else None,
                    )
                with widgets[1]:
                    maximum_end = None if entry.get("manual") else entry.get("source_end")
                    end_options = end_time_options(entry["start"], entry["end"], maximum_end)
                    st.selectbox(
                        "End time",
                        end_options,
                        index=end_options.index(entry["end"]),
                        key=f"end_{i}",
                        disabled=locked,
                        on_change=on_end_change,
                        args=(i,),
                        help="Limited to 3 hours and the detected attendance boundary.",
                    )
                with widgets[2]:
                    project_options = [""] + [str(p["value"]) for p in st.session_state["projects"]]
                    project_labels = {str(p["value"]): p["text"] for p in st.session_state["projects"]}
                    selected_project = str(entry["project_id"]) if entry.get("project_id") else ""
                    st.selectbox(
                        "Project",
                        project_options,
                        index=project_options.index(selected_project) if selected_project in project_options else 0,
                        format_func=lambda value, labels=project_labels: labels.get(value, "— Select project —"),
                        key=f"project_{i}",
                        on_change=on_project_change,
                        args=(i,),
                    )
                with widgets[3]:
                    if not entry.get("project_id"):
                        st.selectbox("Task", [""], format_func=lambda _: "— Select a project first —", disabled=True, key=f"task_placeholder_{i}")
                    else:
                        pid = str(entry["project_id"])
                        cache = st.session_state["tasks_cache"]
                        slot = st.empty()
                        if pid not in cache:
                            slot.selectbox("Task", [""], format_func=lambda _: "Loading tasks…", disabled=True, key=f"task_loading_{i}")
                            try:
                                cache[pid] = get_worker().fetch_tasks(pid)
                                save_state()
                            except Exception as e:
                                cache[pid] = None
                                st.toast(f"Could not load tasks: {e}", icon="⚠️")
                        tasks = cache.get(pid)
                        if tasks is None:
                            slot.selectbox("Task", [""], format_func=lambda _: "Tasks could not be loaded", disabled=True, key=f"task_error_{i}")
                            if st.button("Retry tasks", key=f"retry_tasks_{i}"):
                                cache.pop(pid, None)
                                st.rerun()
                        elif not tasks:
                            slot.selectbox("Task", [""], format_func=lambda _: "No tasks found", disabled=True, key=f"task_placeholder_{i}")
                        else:
                            task_options = [""] + [str(t["id"]) for t in tasks]
                            task_labels = {str(t["id"]): format_task(t) for t in tasks}
                            selected_task = str(entry["task_id"]) if entry.get("task_id") else ""
                            with slot:
                                st.selectbox(
                                    "Task",
                                    task_options,
                                    index=task_options.index(selected_task) if selected_task in task_options else 0,
                                    format_func=lambda value, labels=task_labels: labels.get(value, "— Select task —"),
                                    key=f"task_{i}",
                                    on_change=on_task_change,
                                    args=(i, tasks),
                                )

                validation_error = validate_entry(i)
                if validation_error:
                    st.error(validation_error, icon="⚠️")

                bottom = st.columns([5.6, 1.7, 1.2, 1.5], vertical_alignment="bottom")
                with bottom[0]:
                    st.text_input(
                        "Remark / description",
                        value=entry.get("remark", ""),
                        key=f"remark_{i}",
                        placeholder="What did you work on during this block?",
                        on_change=on_remark_change,
                        args=(i,),
                    )
                details_ready = bool(entry.get("project_id") and entry.get("task_id") and entry.get("remark", "").strip())
                with bottom[1]:
                    if st.button(
                        "Apply below",
                        key=f"apply_below_{i}",
                        disabled=not details_ready or i == len(entries) - 1,
                        use_container_width=True,
                        help="Copy project, task, and remark to later pending rows.",
                    ):
                        copy_details_to_below(i)
                        st.rerun()
                with bottom[2]:
                    if st.button("Skip", key=f"skip_{i}", use_container_width=True):
                        toggle_skip(i)
                        st.rerun()
                with bottom[3]:
                    save_clicked = st.button(
                        "💾 Save",
                        key=f"save_{i}",
                        type="primary",
                        disabled=not entry_is_ready(i),
                        use_container_width=True,
                        help="Complete the project, task, remark, and resolve time conflicts first.",
                    )

                if save_clicked:
                    with st.spinner(f"Saving Entry {i + 1} to KaryaKeeper..."):
                        if do_save_entry(i):
                            st.rerun()

        if all(e.get("saved") or e.get("skipped") for e in entries):
            st.success("All rows are complete. Saved entries are in KaryaKeeper; skipped rows stayed local.", icon="✅")
