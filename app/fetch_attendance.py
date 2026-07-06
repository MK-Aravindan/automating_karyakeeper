import os
import re
import argparse
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright


def greythr_login(page, domain, username, password):
    """Logs into GreytHR and waits for the ESS portal to load. A context that is
    already signed in redirects straight to the portal and skips the form."""
    page.goto(f"https://{domain}.greythr.com", timeout=30000)
    if "/portal/ess" in page.url:
        return

    page.locator("input[name='username'], input[id='username'], input[placeholder*='Login']").first.fill(username)
    page.locator("input[type='password']").first.fill(password)
    page.keyboard.press("Enter")

    # Wait for the post-login ESS portal URL; the dashboard does not always
    # render a "Sign Out" text element, so waiting on text is unreliable
    page.wait_for_url("**/portal/ess/**", timeout=60000)


def greythr_extract_emp_id(page):
    """Opens Attendance Info and sniffs the internal employee id from the API
    request the page fires. Raises if the id could not be captured."""
    def is_employee_request(request):
        return "employee=" in request.url and "attendance/info/period/current" in request.url

    try:
        with page.expect_request(is_employee_request, timeout=15000) as request_info:
            page.get_by_role("complementary").locator("a").filter(has_text=re.compile(r"^Attendance$")).click()
            page.locator("a.secondary-link:has-text('Attendance Info')").first.click()
        request = request_info.value
        match = re.search(r"employee=(\d+)", request.url)
        if match:
            return match.group(1)
    except Exception as error:
        raise RuntimeError("Could not extract the internal employee ID from GreytHR.") from error

    raise RuntimeError("Could not extract the internal employee ID from GreytHR.")


def greythr_fetch_swipes(page, domain, emp_id, target_date):
    """Returns the raw swipe list for target_date (may be empty). Raises if the API call fails."""
    response = page.request.get(
        f"https://{domain}.greythr.com/latte/v3/attendance/info/{emp_id}/swipes",
        params={
            "startDate": target_date,
            "endDate": "",
            "systemSwipes": "true",
            "swipePairs": "true",
        },
        timeout=30000,
    )
    if not response.ok:
        raise RuntimeError(f"GreytHR swipes API failed: {response.status} {response.status_text}")
    swipes = response.json().get("swipe", [])
    swipes.sort(key=lambda x: x.get("punchDateTime", ""))
    return swipes


def fetch_in_out_time(domain, username, password, target_date):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        print("Logging in to GreytHR...")
        try:
            greythr_login(page, domain, username, password)
            print("Login successful. Extracting internal employee ID...")
        except Exception as e:
            print("Failed to login or dashboard didn't load:", e)
            print("Please check your internet connection and the GREYTHR_USERNAME and GREYTHR_PASSWORD in the .env file.")
            browser.close()
            return

        try:
            emp_id = greythr_extract_emp_id(page)
        except Exception as e:
            print("Failed to navigate to Attendance Info:", e)
            browser.close()
            return

        print(f"Extracted Internal Employee ID: {emp_id}")

        print(f"Fetching attendance for {target_date}...")
        try:
            swipes = greythr_fetch_swipes(page, domain, emp_id, target_date)
        except Exception as e:
            print("Failed to fetch swipes from GreytHR:", e)
            browser.close()
            return

        result = None

        if swipes:
            print(f"\n--- Attendance Details for {target_date} ---")

            current_in = None
            current_out = None
            last_out_time = None

            for s in swipes:
                is_in = s.get("inOutIndicator") == 1
                dt_str = s.get("punchDateTime")
                if not dt_str:
                    continue

                # Strip milliseconds if present
                if "." in dt_str:
                    dt_str = dt_str.split(".")[0]

                try:
                    dt_obj = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("Asia/Kolkata"))
                except ValueError:
                    continue

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

            result = {"swipes": swipes}
        else:
            print(f"No swipes found for {target_date}.")

        browser.close()
        return result

def main():
    import karyakeeper_core as kkc

    kkc.ensure_local_storage()
    load_dotenv(kkc.CONFIG_FILE)
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
