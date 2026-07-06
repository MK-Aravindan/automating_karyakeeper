import os
import re
import argparse
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

def format_utc_to_ist(time_str):
    if not time_str:
        return "Not Yet Out"
    if "." in time_str:
        time_str = time_str.split(".")[0]
    dt = datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%S")
    dt_utc = dt.replace(tzinfo=ZoneInfo("UTC"))
    dt_ist = dt_utc.astimezone(ZoneInfo("Asia/Kolkata"))
    return dt_ist.strftime("%Y-%m-%d %I:%M:%S %p IST")

def fetch_in_out_time(domain, username, password, target_date):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        print("Logging in to GreytHR...")
        page.goto(f"https://{domain}.greythr.com")
        
        try:
            page.locator("input[name='username'], input[id='username'], input[placeholder*='Login']").first.fill(username)
            page.locator("input[type='password']").first.fill(password)
            page.keyboard.press("Enter")
            
            # Wait for dashboard
            page.wait_for_selector("text=Sign Out", timeout=15000)
            print("Login successful. Extracting internal employee ID...")
        except Exception as e:
            print("Failed to login or dashboard didn't load:", e)
            browser.close()
            return

        internal_emp_id = []

        def on_request(request):
            if "employee=" in request.url and "attendance/info/period/current" in request.url:
                match = re.search(r"employee=(\d+)", request.url)
                if match:
                    internal_emp_id.append(match.group(1))

        page.on("request", on_request)

        # Trigger the request that contains the employee ID
        try:
            page.get_by_role("complementary").locator("a").filter(has_text=re.compile(r"^Attendance$")).click()
            page.wait_for_timeout(1000)
            page.locator("a.secondary-link:has-text('Attendance Info')").first.click()
            
            # Wait a few seconds for the request to be intercepted
            for _ in range(10):
                if internal_emp_id:
                    break
                page.wait_for_timeout(500)
                
        except Exception as e:
            print("Failed to navigate to Attendance Info:", e)
            browser.close()
            return

        if not internal_emp_id:
            print("Could not extract internal employee ID.")
            browser.close()
            return

        emp_id = internal_emp_id[0]
        print(f"Extracted Internal Employee ID: {emp_id}")

        # Now make the API call to get the swipes for the target date
        swipes_url = f"https://{domain}.greythr.com/latte/v3/attendance/info/{emp_id}/swipes"
        params = {
            "startDate": target_date,
            "endDate": "",
            "systemSwipes": "true",
            "swipePairs": "true"
        }

        print(f"Fetching attendance for {target_date}...")
        response = page.request.get(swipes_url, params=params)
        
        if response.ok:
            data = response.json()
            swipes = data.get("swipe", [])
            
            if swipes:
                swipes.sort(key=lambda x: x.get("punchDateTime", ""))
                
                print(f"\n--- Attendance Details for {target_date} ---")
                
                current_in = None
                current_out = None
                last_out_time = None
                
                for s in swipes:
                    is_in = s.get("inOutIndicator") == 1
                    dt_str = s.get("punchDateTime")
                    if not dt_str:
                        continue
                    
                    if "." in dt_str:
                        dt_str = dt_str.split(".")[0]
                    dt_obj = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("Asia/Kolkata"))
                    
                    if is_in:
                        current_in = dt_obj
                        if last_out_time:
                            break_duration = int((current_in - last_out_time).total_seconds() / 60.0)
                            hours = break_duration // 60
                            mins = break_duration % 60
                            break_str = f"{hours} hr {mins} min" if hours > 0 else f"{mins} min"
                            print(f"\n[ Break: {break_str} ]\n")
                    else:
                        current_out = dt_obj
                        
                    if current_in and current_out:
                        print(f"In-Time:      {current_in.strftime('%Y-%m-%d %I:%M:%S %p IST')}")
                        print(f"Out-Time:     {current_out.strftime('%Y-%m-%d %I:%M:%S %p IST')}")
                        last_out_time = current_out
                        current_in = None
                        current_out = None
                        
                # Handle the case where they are currently working (no final out-time)
                if current_in and not current_out:
                    print(f"In-Time:      {current_in.strftime('%Y-%m-%d %I:%M:%S %p IST')}")
                    print(f"Out-Time:     Not Yet Out")
                    
                print("-------------------------------------------\n")
                
                return {
                    "swipes": swipes
                }
            else:
                print(f"No swipes found for {target_date}.")
        else:
            print(f"Failed to fetch swipes API: {response.status} {response.status_text}")
            print(response.text())
            
        browser.close()

def main():
    load_dotenv()
    domain = os.getenv("GREYTHR_DOMAIN")
    username = os.getenv("GREYTHR_USERNAME")
    password = os.getenv("GREYTHR_PASSWORD")

    if not all([domain, username, password]):
        print("Missing required environment variables in .env.")
        print("Please ensure GREYTHR_DOMAIN, GREYTHR_USERNAME, and GREYTHR_PASSWORD are set.")
        return
        
    parser = argparse.ArgumentParser(description="Fetch In-Time and Out-Time from GreytHR for a specific date using Playwright.")
    parser.add_argument("--date", type=str, required=True, help="Date to fetch attendance for in YYYY-MM-DD format (e.g., 2023-10-25).")
    args = parser.parse_args()

    fetch_in_out_time(domain, username, password, args.date)

if __name__ == "__main__":
    main()
