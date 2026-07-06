import os
import sys
import argparse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from fetch_attendance import fetch_in_out_time
import karyakeeper_core as kkc

SCRIPT_DIR = kkc.SCRIPT_DIR
ROOT_DIR = kkc.ROOT_DIR


def main():
    kkc.ensure_local_storage()
    load_dotenv(kkc.CONFIG_FILE)

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
        if not kkc.validate_date(args.date):
            print(f"Invalid date format: '{args.date}'. Please use YYYY-MM-DD (e.g. 2026-07-06).")
            return
        target_date = args.date
    else:
        target_date = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")

    print(f"\n[1/4] Fetching attendance for {target_date}...")
    attendance = fetch_in_out_time(gt_domain, gt_user, gt_pass, target_date)

    if not attendance or not attendance.get("swipes"):
        print("No punches found for this date. Exiting.")
        kkc.cleanup_auth_files()
        return

    swipes = attendance["swipes"]
    blocks = kkc.consolidate_blocks(swipes)

    if not blocks:
        print("Could not build any work blocks from the swipe data. Exiting.")
        kkc.cleanup_auth_files()
        return

    print("\n[2/4] Logging into KaryaKeeper...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        try:
            context = browser.new_context()
            page = context.new_page()

            try:
                kkc.login_karyakeeper(context, page, kk_url, kk_user, kk_pass)
            except Exception as e:
                print("Failed to log into KaryaKeeper:", e)
                print("Please check your KARYAKEEPER_USERNAME and KARYAKEEPER_PASSWORD in the .env file.")
                browser.close()
                kkc.cleanup_auth_files()
                return

            print(f"\n[3/4] Fetching existing entries for {target_date} from KaryaKeeper...")
            existing_times = kkc.fetch_existing_entries(page, kk_url, target_date)

            if existing_times:
                print("Found existing time entries:")
                for s, e in existing_times:
                    print(f"  - {s} to {e}")
            else:
                print("No existing time entries found.")

            filtered_blocks = kkc.filter_existing_blocks(blocks, existing_times, target_date)
            chunked_blocks = kkc.process_and_chunk_blocks(filtered_blocks)

            if not chunked_blocks:
                print("\nAll time blocks for this date have already been logged. Exiting.")
                browser.close()
                kkc.cleanup_auth_files()
                return

            print("\nFiltered & Chunked Work Blocks (Max 3 hours):")
            for idx, (start_dt, end_dt, is_running) in enumerate(chunked_blocks):
                start_str = start_dt.strftime("%I:%M %p")
                end_str = end_dt.strftime("%I:%M %p")
                running_label = " (Ongoing)" if is_running else ""
                print(f"  Block {idx+1}: {start_str} to {end_str}{running_label}")

            print("\nNavigating to Timesheet creation...")
            dt_obj = datetime.strptime(target_date, "%Y-%m-%d")
            projects = kkc.fetch_projects(page, kk_url)
            if not projects:
                print("No projects found! Please check your KaryaKeeper account.")
                browser.close()
                kkc.cleanup_auth_files()
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
                            kkc.cleanup_auth_files()
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
                        kkc.cleanup_auth_files()
                        return

                # --- Task fetch ---
                print("Loading tasks...")
                try:
                    tasks = kkc.fetch_tasks(page, kk_url, selected_project)
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
                            kkc.cleanup_auth_files()
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
                        kkc.cleanup_auth_files()
                        return

                try:
                    remark = input("\nEnter task description / remark: ")
                except (KeyboardInterrupt, EOFError):
                    print("\nExiting...")
                    browser.close()
                    kkc.cleanup_auth_files()
                    return

                kkc.fill_and_submit_timesheet_form(page, kk_date, start_rounded, end_rounded, remark, selected_project, selected_task_id, selected_task_title)

                try:
                    kkc.confirm_timesheet_log(page)
                    print(f"-> Logged entry {current_idx+1}/{len(chunked_blocks)} to KaryaKeeper.")
                except Exception as e:
                    print(f"Failed to log entry {current_idx+1} to KaryaKeeper.", e)

                current_idx += 1

                # Reload a fresh entry form for the next block
                if current_idx < len(chunked_blocks):
                    page.goto(
                        kk_url.rstrip('/') + "/timesheet?action=create",
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    page.wait_for_selector("#logProjects option", state="attached", timeout=30000)

            print("\nAll entries have been processed.")

        except Exception as e:
            print(f"\nUnexpected error: {e}")
        finally:
            browser.close()
            kkc.cleanup_auth_files()

if __name__ == "__main__":
    main()
