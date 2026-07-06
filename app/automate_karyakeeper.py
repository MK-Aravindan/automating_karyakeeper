import os
import argparse
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from fetch_attendance import fetch_in_out_time

def round_dt_15_mins(dt):
    if not dt:
        return None
    minute = (dt.minute // 15) * 15
    if dt.minute % 15 > 7:
        minute += 15
    dt_rounded = dt.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=minute)
    return dt_rounded

def consolidate_blocks(swipes):
    blocks = []
    current_in = None
    current_out = None
    
    for swipe in sorted(swipes, key=lambda x: x['punchDateTime']):
        pt_utc = datetime.strptime(swipe['punchDateTime'], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
        pt = pt_utc.astimezone(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
        indicator = swipe['inOutIndicator']
        
        if indicator == 1: # IN
            if current_in is None:
                current_in = pt
            else:
                if current_out is not None:
                    diff = (pt - current_out).total_seconds() / 60.0
                    if diff > 15:
                        blocks.append((current_in, current_out))
                        current_in = pt
                    current_out = None
        else: # OUT
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
        except Exception as e:
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
    for f in ["auth.json", "kk_auth.json"]:
        if os.path.exists(f):
            try:
                os.remove(f)
            except:
                pass

def main():
    load_dotenv()
    
    gt_domain = os.getenv("GREYTHR_DOMAIN")
    gt_user = os.getenv("GREYTHR_USERNAME")
    gt_pass = os.getenv("GREYTHR_PASSWORD")
    kk_url = os.getenv("KARYAKEEPER_URL")
    kk_user = os.getenv("KARYAKEEPER_USERNAME")
    kk_pass = os.getenv("KARYAKEEPER_PASSWORD")

    if not all([gt_domain, gt_user, gt_pass, kk_url, kk_user, kk_pass]):
        print("Missing credentials in .env file.")
        cleanup_auth_files()
        return

    parser = argparse.ArgumentParser(description="Automate KaryaKeeper timesheet entry.")
    parser.add_argument("--date", type=str, required=False, help="Date in YYYY-MM-DD format (defaults to today).")
    args = parser.parse_args()
    
    if args.date:
        target_date = args.date
    else:
        # Default to today's date in IST
        target_date = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")

    print(f"\n[1/4] Fetching attendance for {target_date}...")
    attendance = fetch_in_out_time(gt_domain, gt_user, gt_pass, target_date)
    
    if not attendance or not attendance.get("swipes"):
        print("No punches found for this date. Exiting.")
        cleanup_auth_files()
        return
        
    swipes = attendance["swipes"]
    blocks = consolidate_blocks(swipes)

    print("\n[2/4] Logging into KaryaKeeper...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        
        if os.path.exists("kk_auth.json"):
            context = browser.new_context(storage_state="kk_auth.json")
        else:
            context = browser.new_context()
            
        page = context.new_page()
        page.goto(kk_url)
        
        try:
            page.wait_for_selector("text=Dashboard", timeout=5000)
            needs_login = False
        except:
            needs_login = True

        if needs_login:
            page.locator("#login-email, input[name='email']").first.fill(kk_user)
            page.locator("#login-password, input[name='password']").first.fill(kk_pass)
            page.keyboard.press("Enter")
            
            try:
                page.wait_for_selector("text=Dashboard", timeout=30000)
                context.storage_state(path="kk_auth.json")
            except Exception as e:
                print("Failed to log into KaryaKeeper:", e)
                browser.close()
                return

        print(f"\n[3/4] Fetching existing entries for {target_date} from KaryaKeeper...")
        page.goto(kk_url.rstrip('/') + "/timesheet")
        page.wait_for_load_state("networkidle")
        
        dt_obj = datetime.strptime(target_date, "%Y-%m-%d")
        target_date_str = dt_obj.strftime("%d %B %Y") # e.g. "06 July 2026"
        
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
            print("No projects found!")
            browser.close()
            cleanup_auth_files()
            return

        print("\n[4/4] Timesheet Details")
        
        dt_obj = datetime.strptime(target_date, "%Y-%m-%d")
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
                
            while True:
                try:
                    p_input = input("Select Project number (or '0' to edit time, 'q' to quit): ")
                    if p_input.lower() == 'q':
                        print("Exiting...")
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
                                    year=start_dt.year, month=start_dt.month, day=start_dt.day, tzinfo=start_dt.tzinfo)
                                start_dt = new_start_dt
                                start_rounded = start_dt.strftime("%H:%M")
                            
                            if new_end:
                                new_end_dt = datetime.strptime(new_end, "%H:%M").replace(
                                    year=end_dt.year, month=end_dt.month, day=end_dt.day, tzinfo=end_dt.tzinfo)
                                
                                # If the new end time is earlier than original, save the remainder block for later!
                                if new_end_dt < end_dt:
                                    chunked_blocks.insert(current_idx + 1, (new_end_dt, end_dt, is_running))
                                
                                end_dt = new_end_dt
                                end_rounded = end_dt.strftime("%H:%M")
                                
                            chunked_blocks[current_idx] = (start_dt, end_dt, is_running)
                            print(f"\nTime successfully updated to: {start_rounded} to {end_rounded}")
                            print("-----------------------------------")
                        except ValueError:
                            print("Invalid time format. Please use HH:MM (24-hour format).")
                        continue
                        
                    p_idx = int(p_input) - 1
                    if p_idx < 0: raise ValueError
                    selected_project = projects[p_idx]["value"]
                    break
                except (ValueError, IndexError):
                    print("Invalid selection. Please enter a valid number.")
                except (KeyboardInterrupt, EOFError):
                    print("\nExiting...")
                    browser.close()
                    cleanup_auth_files()
                    return
                    
            print("Loading tasks...")
            task_res = page.request.get(f"{kk_url.rstrip('/')}/project/timesheet/task?projectId={selected_project}")
            if not task_res.ok:
                print("Failed to fetch tasks from API.")
                continue
                
            tasks = task_res.json().get("results", [])
            if not tasks:
                print("No tasks found for this project!")
                continue
                
            print("\nAvailable Tasks:")
            for i, task in enumerate(tasks):
                group = task.get("group_name", "")
                title = task.get("title", "")
                display = f"[{group}] {title}" if group else title
                print(f"  {i+1}. {display}")
                
            while True:
                try:
                    t_input = input("Select Task number (or 'q' to quit): ")
                    if t_input.lower() == 'q':
                        print("Exiting...")
                        browser.close()
                        cleanup_auth_files()
                        return
                    t_idx = int(t_input) - 1
                    if t_idx < 0: raise ValueError
                    selected_task_id = tasks[t_idx]["id"]
                    selected_task_title = tasks[t_idx]["title"]
                    break
                except (ValueError, IndexError):
                    print("Invalid selection. Please enter a valid number.")
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
            
            # Escape strings for JS
            remark_esc = remark.replace("'", "\\'").replace('"', '\\"')
            task_title_esc = selected_task_title.replace("'", "\\'").replace('"', '\\"')
            
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
            print(f"-> Added block {current_idx+1} to timesheet list.")
            
            current_idx += 1

        # Confirm all
        try:
            print("\nSubmitting all entries to KaryaKeeper server...")
            page.locator("#log_button").click()
            page.wait_for_timeout(3000)
            print("Successfully saved all timesheet entries!")
        except Exception as e:
            print("Failed to click final 'Log Entrie(s)' button.", e)
            
        browser.close()
        cleanup_auth_files()

if __name__ == "__main__":
    main()
