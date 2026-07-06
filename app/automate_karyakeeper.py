import os
import sys
import argparse
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from fetch_attendance import fetch_in_out_time

# Resolve paths relative to THIS script's location, not the working directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)

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

def main():
    load_dotenv(os.path.join(ROOT_DIR, ".env"))

    gt_domain = os.getenv("GREYTHR_DOMAIN")
    gt_user = os.getenv("GREYTHR_USERNAME")
    gt_pass = os.getenv("GREYTHR_PASSWORD")
    kk_url = os.getenv("KARYAKEEPER_URL")
    kk_user = os.getenv("KARYAKEEPER_USERNAME")
    kk_pass = os.getenv("KARYAKEEPER_PASSWORD")

    missing = [k for k, v in {
        "GREYTHR_DOMAIN": gt_domain,
        "GREYTHR_USERNAME": gt_user,
        "GREYTHR_PASSWORD": gt_pass,
        "KARYAKEEPER_URL": kk_url,
        "KARYAKEEPER_USERNAME": kk_user,
        "KARYAKEEPER_PASSWORD": kk_pass,
    }.items() if not v]

    if missing:
        print(f"Missing credentials in .env file: {', '.join(missing)}")
        print("Please open the .env file and fill in all required values.")
        return

    parser = argparse.ArgumentParser(description="Automate KaryaKeeper timesheet entry.")
    parser.add_argument("--date", type=str, required=False, help="Date in YYYY-MM-DD format (defaults to today).")
    args = parser.parse_args()

    if args.date:
        if not validate_date(args.date):
            print(f"Invalid date format: '{args.date}'. Please use YYYY-MM-DD (e.g. 2026-07-06).")
            return
        target_date = args.date
    else:
        target_date = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")

    print(f"\n[1/4] Fetching attendance for {target_date}...")
    attendance = fetch_in_out_time(gt_domain, gt_user, gt_pass, target_date)

    if not attendance or not attendance.get("swipes"):
        print("No punches found for this date. Exiting.")
        cleanup_auth_files()
        return

    swipes = attendance["swipes"]
    blocks = consolidate_blocks(swipes)

    if not blocks:
        print("Could not build any work blocks from the swipe data. Exiting.")
        cleanup_auth_files()
        return

    print("\n[2/4] Logging into KaryaKeeper...")

    kk_auth_path = os.path.join(ROOT_DIR, "kk_auth.json")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        try:
            if os.path.exists(kk_auth_path):
                context = browser.new_context(storage_state=kk_auth_path)
            else:
                context = browser.new_context()

            page = context.new_page()
            page.goto(kk_url, timeout=30000)

            try:
                page.wait_for_selector("text=Dashboard", timeout=5000)
                needs_login = False
            except Exception:
                needs_login = True

            if needs_login:
                page.locator("#login-email, input[name='email']").first.fill(kk_user)
                page.locator("#login-password, input[name='password']").first.fill(kk_pass)
                page.keyboard.press("Enter")

                try:
                    page.wait_for_selector("text=Dashboard", timeout=30000)
                    context.storage_state(path=kk_auth_path)
                except Exception as e:
                    print("Failed to log into KaryaKeeper:", e)
                    print("Please check your KARYAKEEPER_USERNAME and KARYAKEEPER_PASSWORD in the .env file.")
                    browser.close()
                    cleanup_auth_files()
                    return

            print(f"\n[3/4] Fetching existing entries for {target_date} from KaryaKeeper...")
            page.goto(kk_url.rstrip('/') + "/timesheet")
            page.wait_for_load_state("networkidle")

            dt_obj = datetime.strptime(target_date, "%Y-%m-%d")
            target_date_str = dt_obj.strftime("%d %B %Y")

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

            existing_times = []
            for r in rows:
                if len(r) >= 6:
                    if re.match(r"^\d{2}:\d{2}$", r[4]) and re.match(r"^\d{2}:\d{2}$", r[5]):
                        existing_times.append((r[4], r[5]))

            if existing_times:
                print("Found existing time entries:")
                for s, e in existing_times:
                    print(f"  - {s} to {e}")
            else:
                print("No existing time entries found.")

            filtered_blocks = filter_existing_blocks(blocks, existing_times, target_date)
            chunked_blocks = process_and_chunk_blocks(filtered_blocks)

            if not chunked_blocks:
                print("\nAll time blocks for this date have already been logged. Exiting.")
                browser.close()
                cleanup_auth_files()
                return

            print("\nFiltered & Chunked Work Blocks (Max 3 hours):")
            for idx, (start_dt, end_dt, is_running) in enumerate(chunked_blocks):
                start_str = start_dt.strftime("%I:%M %p")
                end_str = end_dt.strftime("%I:%M %p")
                running_label = " (Ongoing)" if is_running else ""
                print(f"  Block {idx+1}: {start_str} to {end_str}{running_label}")

            print("\nNavigating to Timesheet creation...")
            page.goto(kk_url.rstrip('/') + "/timesheet?action=create")
            page.wait_for_load_state("networkidle")

            projects = page.locator("#logProjects option").evaluate_all("els => els.map(e => ({text: e.innerText.trim(), value: e.value})).filter(e => e.value)")
            if not projects:
                print("No projects found! Please check your KaryaKeeper account.")
                browser.close()
                cleanup_auth_files()
                return

            print("\n[4/4] Timesheet Details")

            kk_date = dt_obj.strftime("%d/%m/%Y")

            current_idx = 0
            while current_idx < len(chunked_blocks):
                start_dt, end_dt, is_running = chunked_blocks[current_idx]
                start_rounded = start_dt.strftime("%H:%M")
                end_rounded = end_dt.strftime("%H:%M")

                if is_running:
                    print(f"\nWarning: Original Block had no Out-Time. Using rounded current time: {end_rounded}")

                print(f"\n--- Entry {current_idx+1}/{len(chunked_blocks)}: {start_rounded} to {end_rounded} ---")

                print("\nAvailable Projects:")
                for i, proj in enumerate(projects):
                    print(f"  {i+1}. {proj['text']}")

                # --- Project selection loop ---
                selected_project = None
                while True:
                    try:
                        p_input = input("Select Project number (or '0' to edit time, 'q' to quit): ").strip()
                        if p_input.lower() == 'q':
                            print("Exiting... Entries already logged have been saved.")
                            browser.close()
                            cleanup_auth_files()
                            return
                        if p_input == '0':
                            print(f"\n--- Edit Time for Entry {current_idx+1} ---")
                            print(f"Current Time: {start_rounded} to {end_rounded}")
                            new_start = input(f"Enter new Start Time (HH:MM) [Press Enter to keep {start_rounded}]: ").strip()
                            new_end = input(f"Enter new End Time (HH:MM) [Press Enter to keep {end_rounded}]: ").strip()
                            try:
                                if new_start:
                                    new_start_dt = datetime.strptime(new_start, "%H:%M").replace(
                                        year=start_dt.year, month=start_dt.month, day=start_dt.day)
                                    start_dt = new_start_dt
                                    start_rounded = start_dt.strftime("%H:%M")
                                if new_end:
                                    new_end_dt = datetime.strptime(new_end, "%H:%M").replace(
                                        year=end_dt.year, month=end_dt.month, day=end_dt.day)
                                    if new_end_dt < end_dt:
                                        chunked_blocks.insert(current_idx + 1, (new_end_dt, end_dt, is_running))
                                    end_dt = new_end_dt
                                    end_rounded = end_dt.strftime("%H:%M")
                                chunked_blocks[current_idx] = (start_dt, end_dt, is_running)
                                print(f"\nTime updated to: {start_rounded} to {end_rounded}")
                            except ValueError:
                                print("Invalid time format. Please use HH:MM (e.g. 09:30).")
                            continue

                        p_idx = int(p_input) - 1
                        if p_idx < 0 or p_idx >= len(projects):
                            raise ValueError
                        selected_project = projects[p_idx]["value"]
                        break
                    except (ValueError, IndexError):
                        print(f"Invalid selection. Please enter a number between 1 and {len(projects)}.")
                    except (KeyboardInterrupt, EOFError):
                        print("\nExiting...")
                        browser.close()
                        cleanup_auth_files()
                        return

                # --- Task fetch ---
                print("Loading tasks...")
                try:
                    task_res = page.request.get(f"{kk_url.rstrip('/')}/project/timesheet/task?projectId={selected_project}")
                    if not task_res.ok:
                        print(f"Failed to fetch tasks (HTTP {task_res.status}). Skipping this block.")
                        current_idx += 1
                        continue
                    tasks = task_res.json().get("results", [])
                except Exception as e:
                    print(f"Error fetching tasks: {e}. Skipping this block.")
                    current_idx += 1
                    continue

                if not tasks:
                    print("No tasks found for this project. Skipping this block.")
                    current_idx += 1
                    continue

                print("\nAvailable Tasks:")
                for i, task in enumerate(tasks):
                    group = task.get("group_name", "")
                    title = task.get("title", "")
                    display = f"[{group}] {title}" if group else title
                    print(f"  {i+1}. {display}")

                # --- Task selection loop ---
                selected_task_id = None
                selected_task_title = None
                while True:
                    try:
                        t_input = input("Select Task number (or 'q' to quit): ").strip()
                        if t_input.lower() == 'q':
                            print("Exiting... Entries already logged have been saved.")
                            browser.close()
                            cleanup_auth_files()
                            return
                        t_idx = int(t_input) - 1
                        if t_idx < 0 or t_idx >= len(tasks):
                            raise ValueError
                        selected_task_id = tasks[t_idx]["id"]
                        selected_task_title = tasks[t_idx]["title"]
                        break
                    except (ValueError, IndexError):
                        print(f"Invalid selection. Please enter a number between 1 and {len(tasks)}.")
                    except (KeyboardInterrupt, EOFError):
                        print("\nExiting...")
                        browser.close()
                        cleanup_auth_files()
                        return

                try:
                    remark = input("\nEnter task description / remark: ")
                except (KeyboardInterrupt, EOFError):
                    print("\nExiting...")
                    browser.close()
                    cleanup_auth_files()
                    return

                # Safely escape strings for JS injection
                remark_esc = js_safe(remark)
                task_title_esc = js_safe(selected_task_title)

                page.evaluate(f"""
                    document.getElementById('date').value = '{kk_date}';
                    document.getElementById('start_time').value = '{start_rounded}';
                    document.getElementById('end_time').value = '{end_rounded}';
                    document.getElementById('remark').value = '{remark_esc}';

                    let taskOption = new Option('{task_title_esc}', '{selected_task_id}', true, true);
                    $('#logTasks').append(taskOption).trigger('change');
                    $('#logProjects').val('{selected_project}').trigger('change');
                """)

                page.locator("#submit_timesheet").click()
                page.wait_for_timeout(1500)

                try:
                    page.locator("#log_button").click()
                    page.wait_for_timeout(3000)
                    print(f"-> Logged entry {current_idx+1}/{len(chunked_blocks)} to KaryaKeeper.")
                except Exception as e:
                    print(f"Failed to log entry {current_idx+1} to KaryaKeeper.", e)

                current_idx += 1

                # Reload a fresh entry form for the next block
                if current_idx < len(chunked_blocks):
                    page.goto(kk_url.rstrip('/') + "/timesheet?action=create")
                    page.wait_for_load_state("networkidle")

            print("\nAll entries have been processed.")

        except Exception as e:
            print(f"\nUnexpected error: {e}")
        finally:
            browser.close()
            cleanup_auth_files()

if __name__ == "__main__":
    main()
