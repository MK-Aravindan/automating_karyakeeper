import queue
import threading
import time
import os
from datetime import datetime

from playwright.sync_api import sync_playwright

import karyakeeper_core as kkc
import fetch_attendance as gt


class BrowserWorker:
    """Runs one long-lived headless browser on a dedicated thread.

    Playwright's sync API is bound to the thread that starts it, while Streamlit
    reruns scripts on varying threads, so every browser call is funnelled through
    a job queue. Keeping the browser alive means GreytHR and KaryaKeeper are each
    logged into once per app run instead of once per click, and the login
    sessions live only in browser memory — nothing is ever written to disk."""

    def __init__(self, gt_domain, gt_user, gt_pass, kk_url, kk_user, kk_pass):
        self.gt_domain = gt_domain
        self.gt_user = gt_user
        self.gt_pass = gt_pass
        self.kk_url = kk_url
        self.kk_user = kk_user
        self.kk_pass = kk_pass

        self.jobs = queue.Queue()
        self.pw = None
        self.browser = None
        self.gt_page = None
        self.gt_emp_id = None
        self.gt_emp_id_source = None
        self.kk_page = None
        self.start_error = None
        self.ready = threading.Event()
        self.timings = {}
        self.timings_lock = threading.Lock()
        self.runtime_cache_file = os.path.join(kkc.DATA_DIR, "runtime_cache.json")
        self.thread = threading.Thread(target=self.job_loop, name="browser-worker", daemon=True)
        self.thread.start()
        if not self.ready.wait(timeout=60):
            raise RuntimeError("The browser worker did not start within 60 seconds.")
        if self.start_error:
            raise RuntimeError(f"The browser worker could not start: {self.start_error}") from self.start_error

    # ------------------------------------------------------------- public API

    # Each cap must comfortably exceed the worst-case sum of the Playwright
    # timeouts inside the job (login redirects alone can take 60s), so a slow
    # day never reports failure while the browser is still legitimately working.
    def fetch_swipes(self, target_date):
        return self.submit(self.run_fetch_swipes, target_date, timeout=300)

    def ensure_karyakeeper_login(self):
        return self.submit(self.ensure_kk, timeout=150)

    def fetch_existing_entries(self, target_date):
        return self.submit(self.run_fetch_existing, target_date, timeout=180)

    def fetch_projects(self):
        return self.submit(self.run_fetch_projects, timeout=180)

    def fetch_karyakeeper_context(self, target_date):
        return self.submit(self.run_fetch_karyakeeper_context, target_date, timeout=300)

    def fetch_tasks(self, project_id):
        return self.submit(self.run_fetch_tasks, project_id, timeout=90)

    def save_entry(self, kk_date, start, end, remark, project_id, task_id, task_title):
        return self.submit(
            self.run_save_entry, kk_date, start, end, remark, project_id, task_id, task_title,
            timeout=360,
        )

    def timing_snapshot(self):
        with self.timings_lock:
            return dict(self.timings)

    @property
    def gt_session_active(self):
        return self.gt_emp_id is not None

    @property
    def kk_session_active(self):
        return self.kk_page is not None

    def close(self):
        if not self.thread.is_alive():
            return
        done = threading.Event()
        self.jobs.put((self.shutdown, (), {}, done))
        done.wait(timeout=15)

    # -------------------------------------------------------------- plumbing

    def submit(self, fn, *args, timeout=60):
        if not self.thread.is_alive():
            raise RuntimeError("The browser worker has stopped. Close the terminal window and start run.bat again.")
        done = threading.Event()
        box = {}
        self.jobs.put((fn, args, box, done))
        if not done.wait(timeout=timeout):
            raise RuntimeError(
                f"{fn.__name__} did not finish within {timeout} seconds. "
                "Do not repeat a save immediately; restart the app and refresh attendance first."
            )
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def job_loop(self):
        try:
            with sync_playwright() as pw:
                self.pw = pw
                self.ready.set()
                while True:
                    fn, args, box, done = self.jobs.get()
                    started = time.perf_counter()
                    try:
                        box["result"] = fn(*args)
                    except Exception as e:
                        box["error"] = e
                    finally:
                        elapsed = time.perf_counter() - started
                        with self.timings_lock:
                            self.timings[fn.__name__] = round(elapsed, 3)
                        done.set()
                    if fn == self.shutdown:
                        return
        except Exception as error:
            self.start_error = error
            self.ready.set()

    def get_browser(self):
        if self.browser and self.browser.is_connected():
            return self.browser
        self.browser = self.pw.chromium.launch(headless=True)
        self.gt_page = None
        self.gt_emp_id = None
        self.gt_emp_id_source = None
        self.kk_page = None
        return self.browser

    def shutdown(self):
        if self.browser and self.browser.is_connected():
            self.browser.close()

    # --------------------------------------------------------------- GreytHR

    def drop_gt(self):
        if self.gt_page:
            try:
                self.gt_page.context.close()
            except Exception:
                pass
        self.gt_page = None
        self.gt_emp_id = None
        self.gt_emp_id_source = None

    def load_cached_employee_id(self):
        try:
            data, _ = kkc.load_json_with_backup(self.runtime_cache_file)
            value = data.get("greythr_employee_id") if isinstance(data, dict) else None
            return str(value) if value else None
        except kkc.StateStoreError:
            return None

    def save_cached_employee_id(self, employee_id):
        kkc.atomic_write_json(self.runtime_cache_file, {"greythr_employee_id": str(employee_id)})

    def ensure_gt(self):
        if self.gt_page is None or self.gt_page.is_closed() or self.gt_emp_id is None:
            self.drop_gt()
            page = self.get_browser().new_context().new_page()
            gt.greythr_login(page, self.gt_domain, self.gt_user, self.gt_pass)
            self.gt_page = page
            cached_id = self.load_cached_employee_id()
            if cached_id:
                self.gt_emp_id = cached_id
                self.gt_emp_id_source = "cache"
            else:
                self.gt_emp_id = gt.greythr_extract_emp_id(page)
                self.gt_emp_id_source = "browser"
                self.save_cached_employee_id(self.gt_emp_id)

    def run_fetch_swipes(self, target_date):
        # Retry once only when reusing an old session (it may have expired);
        # a failure right after a fresh login is a real error
        had_session = self.gt_session_active
        try:
            self.ensure_gt()
            return gt.greythr_fetch_swipes(self.gt_page, self.gt_domain, self.gt_emp_id, target_date)
        except Exception:
            if self.gt_emp_id_source == "cache" and self.gt_page and not self.gt_page.is_closed():
                self.gt_emp_id = gt.greythr_extract_emp_id(self.gt_page)
                self.gt_emp_id_source = "browser"
                self.save_cached_employee_id(self.gt_emp_id)
                return gt.greythr_fetch_swipes(self.gt_page, self.gt_domain, self.gt_emp_id, target_date)
            if not had_session:
                raise
            self.drop_gt()
            self.ensure_gt()
            return gt.greythr_fetch_swipes(self.gt_page, self.gt_domain, self.gt_emp_id, target_date)

    # ------------------------------------------------------------ KaryaKeeper

    def drop_kk(self):
        if self.kk_page:
            try:
                self.kk_page.context.close()
            except Exception:
                pass
        self.kk_page = None

    def ensure_kk(self):
        if self.kk_page is None or self.kk_page.is_closed():
            self.drop_kk()
            context = self.get_browser().new_context()
            page = context.new_page()
            kkc.login_karyakeeper(context, page, self.kk_url, self.kk_user, self.kk_pass)
            self.kk_page = page

    def with_kk_retry(self, op):
        had_session = self.kk_session_active
        try:
            self.ensure_kk()
            return op()
        except Exception:
            if not had_session:
                raise
            self.drop_kk()
            self.ensure_kk()
            return op()

    def run_fetch_existing(self, target_date):
        return self.with_kk_retry(lambda: kkc.fetch_existing_entries_detailed(self.kk_page, self.kk_url, target_date))

    def run_fetch_projects(self):
        return self.with_kk_retry(lambda: kkc.fetch_projects(self.kk_page, self.kk_url))

    def run_fetch_karyakeeper_context(self, target_date):
        def operation():
            existing = kkc.fetch_existing_entries_detailed(self.kk_page, self.kk_url, target_date)
            projects = kkc.fetch_projects(self.kk_page, self.kk_url)
            return existing, projects

        return self.with_kk_retry(operation)

    def run_fetch_tasks(self, project_id):
        return self.with_kk_retry(lambda: kkc.fetch_tasks(self.kk_page, self.kk_url, project_id))

    def run_save_entry(self, kk_date, start, end, remark, project_id, task_id, task_title):
        # Only opening the form is retried; the submit itself never is, so an
        # entry can never be double-logged by a retry
        self.with_kk_retry(self.open_create_form)
        kkc.fill_and_submit_timesheet_form(self.kk_page, kk_date, start, end, remark, project_id, task_id, task_title)
        try:
            kkc.confirm_timesheet_log(self.kk_page)
            return {"status": "confirmed"}
        except Exception as confirmation_error:
            target_date = datetime.strptime(kk_date, "%d/%m/%Y").strftime("%Y-%m-%d")
            try:
                existing = kkc.fetch_existing_entries_detailed(self.kk_page, self.kk_url, target_date)
                committed = any(
                    entry["start"] == start
                    and entry["end"] == end
                    and entry.get("remark", "").strip() == remark.strip()
                    for entry in existing
                )
            except Exception as verification_error:
                raise RuntimeError(
                    "KaryaKeeper did not return a clear save confirmation and the result could not be verified. "
                    "Refresh attendance before retrying to avoid a duplicate entry."
                ) from verification_error
            if committed:
                return {"status": "verified_after_timeout"}
            raise RuntimeError(
                "KaryaKeeper did not confirm the save and no matching entry was found. "
                "Refresh attendance before trying again."
            ) from confirmation_error

    def open_create_form(self):
        page = self.kk_page
        page.goto(
            self.kk_url.rstrip('/') + "/timesheet?action=create",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        form = page.locator("#logProjects")
        login_box = page.locator("#login-email, input[name='email']").first
        form.or_(login_box).first.wait_for(state="attached", timeout=30000)
        if login_box.count():
            raise RuntimeError("KaryaKeeper session expired.")
        # The form is filled through jQuery/select2, so both must be ready
        page.wait_for_function(
            "typeof $ !== 'undefined' && document.querySelectorAll('#logProjects option').length > 0",
            timeout=30000,
        )
        page.wait_for_load_state("load")
