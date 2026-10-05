# BSE KID Scraper

Every morning at 08:00 this tool:

1. Queries BSE **Memorandum of Private Placement Bonds** for yesterday's allotment date.
2. Downloads every disclosure ZIP listed.
3. Unzips them recursively (`Main.zip → Inner.zip → 1.Kid.pdf`, any depth) and finds every **Key Information Document (KID)**.
4. Sends each KID to the **Claude API**, which returns a fixed set of fields (coupon, tenor, rating, issue size, dates and so on).
5. Emails a formatted report (a summary table, one detail card per issue and a CSV attachment) to your recipients.

Each document is processed only once, and a failure in one file never stops the rest.

---

## 1. Architecture

```
            ┌────────────────────── Windows Task Scheduler (daily 08:00) ──────────────────────┐
            │                         scripts\run_pipeline.bat                                  │
            └──────────────────────────────────┬───────────────────────────────────────────────┘
                                               ▼
  src/main.py (CLI) ──► src/pipeline.py (orchestrator, run lock, per-item error isolation)
                              │
      ┌───────────────────────┼─────────────────────────┬──────────────────────────┬────────────────────┐
      ▼                       ▼                         ▼                          ▼                    ▼
 bse_scraper.py         zip_processor.py         claude_processor.py         email_service.py        state.py
 • JSON API (primary)   • recursive unzip        • PDF → Claude (base64)     • HTML + text + CSV     • SQLite dedup
 • Playwright browser   • KID by name/content    • forced tool-call schema   • SMTP TLS/SSL          • documents / kids
   fallback             • zip-slip/size guards   • retries, 429 handling     • retries → outbox      • runs audit
 • streamed downloads   • SHA-256 per KID        • PDF trimming, pacing      • multi-recipient
```

**How BSE is queried.** When you click Submit on the page, it calls
`https://api.bseindia.com/BseIndiaAPI/api/Debtmemorandumbetadata/w?ddldate=1&fdate=YYYY-MM-DD&tdate=YYYY-MM-DD`
(`ddldate=1` means Allotment Date) and draws the table from the JSON it gets back. The scraper calls that same
endpoint directly, which is faster and more reliable than clicking through the page and also avoids the page's pagination.
If the endpoint ever fails, `SCRAPER_MODE=auto` falls back to a real headless Chromium browser. The browser
opens the page, types the From/To dates, clicks **Submit** and reads the result, the same way you would.

**ZIPs shared across ISINs.** A single ZIP often covers several ISINs. Clean Max, for example, has 5 rows and 1 ZIP.
Rows are grouped by download URL, so each ZIP is downloaded and processed once.

### Data folders

| Folder | Contents | Retention |
|---|---|---|
| `downloads\YYYY-MM-DD\` | Original BSE ZIPs (often 100–150 MB each) | `RETENTION_DAYS` (30) |
| `extracted\YYYY-MM-DD\` | Temporary extraction workspace | `RETENTION_DAYS` (30) |
| `processed\kids\YYYY-MM-DD\<Company>\` | **Archive of every KID PDF** | Forever (or `KID_ARCHIVE_RETENTION_DAYS`) |
| `processed\reports\YYYY-MM-DD\` | HTML/CSV/JSON copy of every report | 90 days |
| `processed\outbox\` | Emails that failed to send (`pending_*.eml`) | Re-sent automatically |
| `processed\state.db` | Duplicate-detection database | **Keep. Do not delete.** |
| `logs\` | `bse_kid.log`, `errors.log`, `task_scheduler.log` | `LOG_RETENTION_DAYS` (90) |

---

## 2. Windows deployment: step by step

Every command below runs in **PowerShell**. To open it, press Start, type *PowerShell* and press Enter.

### Step 1 — Install Python 3.12

1. Download **Python 3.12.x (64-bit)** from https://www.python.org/downloads/windows/.
2. Run the installer. On the first screen:
   - ✅ tick **"Add python.exe to PATH"**
   - Click **"Install Now"**.
3. Check the installation by opening a **new** PowerShell window:
   ```powershell
   py -3.12 --version     # should print Python 3.12.x
   ```

### Step 2 — Put the project in a permanent folder

Use a local folder that is **not** synced by OneDrive or Dropbox, because sync clients lock files while the job runs:

```
C:\Automation\bse-kid-scraper\
```

Unzip the project there. You should see `src\`, `config\`, `scripts\`, `README.md` and the other files directly inside that folder.

### Step 3 — Create the virtual environment and install dependencies

**Option A: one command (recommended)**
```powershell
cd C:\Automation\bse-kid-scraper
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

**Option B: manually**
```powershell
cd C:\Automation\bse-kid-scraper
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium     # browser fallback
copy .env.example .env
```

> Every command below calls `.\.venv\Scripts\python.exe` directly, so you never need to "activate" the environment.

### Step 4 — Create and configure `.env`

```powershell
notepad .env
```
The file is commented line by line. At minimum, set:

```ini
EMAIL_FROM=yourname@gmail.com
SMTP_USERNAME=yourname@gmail.com
EMAIL_TO=you@company.com, colleague@company.com
EMAIL_CC=
LOOKBACK_DAYS=3            # recommended, see "Late uploads" below
```

### Step 5 — Set up the Claude API key

1. Go to https://console.anthropic.com → **Settings → API Keys → Create Key**. Copy the key (it starts with `sk-ant-`).
2. Under **Billing**, make sure the account has credits.
3. Store the key using **one** of these methods:

   **Option A (more secure): Windows Credential Manager.** The key never sits in a text file:
   ```powershell
   .\.venv\Scripts\python.exe -m src.main --set-secret ANTHROPIC_API_KEY
   ```
   Paste the key when asked (the input stays hidden). Leave `ANTHROPIC_API_KEY=` blank in `.env`.

   **Option B: in `.env`.**
   ```ini
   ANTHROPIC_API_KEY=sk-ant-...
   ```
4. The model is set by `CLAUDE_MODEL` (default `claude-sonnet-5-5`). You can change it without touching code.

### Step 6 — Set up email credentials securely

**Gmail**
1. Turn on 2-Step Verification: Google Account → Security.
2. Create an **App Password**: Google Account → Security → *App passwords* → name it "BSE KID". Copy the 16-character password.
3. In `.env`:
   ```ini
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=587
   SMTP_SECURITY=starttls
   SMTP_USERNAME=yourname@gmail.com
   EMAIL_FROM=yourname@gmail.com
   ```
4. Store the app password:
   ```powershell
   .\.venv\Scripts\python.exe -m src.main --set-secret SMTP_PASSWORD
   ```
   Alternatively, put it in `.env` as `SMTP_PASSWORD=...`.

**Microsoft 365 / Outlook:** use `SMTP_HOST=smtp.office365.com`, `SMTP_PORT=587`, `SMTP_SECURITY=starttls`. Your admin may need to enable
"Authenticated SMTP" for the mailbox.

**Protect `.env`:** right-click `.env` → Properties → Security, and make sure only your user account and Administrators have access.

### Step 7 — Test the configuration

```powershell
.\.venv\Scripts\python.exe -m src.main --check
```
This validates `.env`, then tests the BSE page and API, the Claude API key and model, and your SMTP login. **It must end with `CHECK PASSED`.**

```powershell
.\.venv\Scripts\python.exe -m src.main --test-email
```
Check that the test email reaches every recipient, including the spam folder.

### Step 8 — Test the scraper manually with yesterday's date

Run these in order. Each stage is safe to repeat.

```powershell
# a) BSE + download + unzip + KID detection only: no Claude cost, no email
.\.venv\Scripts\python.exe -m src.main --dry-run

# b) Try a date you know has documents (DD-MM-YYYY or YYYY-MM-DD both work)
.\.venv\Scripts\python.exe -m src.main --dry-run --date 29-09-2026

# c) Full processing for ONE document, including Claude, but no email
.\.venv\Scripts\python.exe -m src.main --date 29-09-2026 --limit 1 --skip-email
```
After each run, check the following:
- `processed\kids\<date>\...` contains the KID PDFs.
- `processed\reports\<date>\report_*.html` shows what the email would look like (double-click to open it in a browser).
- `logs\bse_kid.log` has the full trace.

### Step 9 — Full end-to-end test before scheduling

```powershell
.\.venv\Scripts\python.exe -m src.main --date 29-09-2026 --force
```
`--force` ignores duplicate detection, so documents you already tested are processed again. Confirm that:
1. The email arrives at all To/Cc/Bcc recipients.
2. The values in the email match the KID PDF (spot-check 2–3 fields).
3. Running the **same command without `--force`** processes nothing new: the log shows `Duplicate detection: skipping`.

Now test the scheduled path exactly as Task Scheduler will run it:
```powershell
.\scripts\run_pipeline.bat --dry-run
type .\logs\task_scheduler.log
```

### Step 10 — Schedule it daily at 08:00 (Windows Task Scheduler)

Open PowerShell **as Administrator** (right-click → *Run as administrator*):
```powershell
cd C:\Automation\bse-kid-scraper
powershell -ExecutionPolicy Bypass -File .\scripts\register_task.ps1 -Time 08:00
```
You will be asked for your **Windows password** once. Windows stores it securely so the task can run while you are logged off.

The task named **"BSE KID Scraper"** is configured to:

| Setting | Effect |
|---|---|
| Run whether user is logged on or not | Runs with no one logged in and no window open |
| Start when available | If the PC was off at 08:00, runs as soon as it is back on |
| Wake the computer to run | Wakes from sleep (requires "Allow wake timers" in Power Options) |
| Run only if network available | Doesn't start and fail offline |
| Restart 3× every 10 min on failure | Covers transient failures to start |
| Stop after 3 hours | A hung run can't block the next day |
| Don't start a new instance | Never overlaps with itself (the app also has its own lock file) |

**Trigger a run immediately** (the best final test):
```powershell
Start-ScheduledTask -TaskName "BSE KID Scraper"
Get-ScheduledTaskInfo -TaskName "BSE KID Scraper"    # LastTaskResult 0 = success, 1 = partial, 2 = fatal
```

**To change the time**, re-run `register_task.ps1 -Time 07:30` (it updates the task in place) and set `SCHEDULE_TIME` in `.env` to match.
**To remove the task:** `register_task.ps1 -Unregister`.

You can also inspect the task in the GUI: Start → *Task Scheduler* → Task Scheduler Library → **BSE KID Scraper** → *History* tab.

### Step 11 — Make sure it runs when you're not using the PC

- The task runs as your account with "Run whether user is logged on or not", so you do **not** need to be logged in.
- The PC must be **on or asleep** at 08:00, not shut down. With "Start when available", a PC that was off runs the job as soon as it boots.
- Go to Power Options → *Change plan settings* → *Change advanced power settings* → **Sleep → Allow wake timers → Enable**.
- If you change your Windows password, re-run `register_task.ps1`. Otherwise the task fails with "logon failure".
- For a machine that is always on, a small always-on PC or VM is ideal.

### Step 12 — Recommended folder structure and permissions

```
C:\Automation\bse-kid-scraper\        ← your user account: Full control; other users: no access
│   .env                              ← secrets: only you + Administrators
├── .venv\                            ← created by setup
├── src\  config\  scripts\  tests\
├── downloads\  extracted\            ← temporary, auto-cleaned
├── processed\                        ← state.db + KID archive + reports + outbox (back this up)
└── logs\
```
- Keep the project **outside** OneDrive, Desktop or Downloads, which are often synced or cleaned.
- Leave roughly **5 GB free**. Each BSE ZIP is 100–150 MB, and old ones are cleaned after `RETENTION_DAYS`.
- Back up `processed\` (especially `state.db` and `kids\`) if the archive matters to you.

### Step 13 — View logs and troubleshoot

| File | What's in it |
|---|---|
| `logs\bse_kid.log` | Everything: page access, dates, documents found, downloads, ZIP tree, KIDs, Claude requests with token usage, email |
| `logs\errors.log` | Only warnings and errors. **Look here first.** |
| `logs\task_scheduler.log` | Start/end and exit code of each scheduled run |

```powershell
Get-Content .\logs\errors.log -Tail 50
Get-Content .\logs\bse_kid.log -Wait           # live view while a run is in progress
.\.venv\Scripts\python.exe -m src.main --status   # recent runs, failed docs/KIDs, totals
```

| Symptom | Fix |
|---|---|
| `CHECK FAILED … Claude API … 401` | The API key is wrong or revoked. Re-run `--set-secret ANTHROPIC_API_KEY`. |
| `Claude API error 404 … model` | `CLAUDE_MODEL` isn't available on your account. Pick one listed in the Anthropic console. |
| `Claude … 429 / overloaded` | Retried automatically. If it happens often, raise `CLAUDE_MIN_INTERVAL_SEC`. |
| `Email authentication failed` | Gmail needs an **App Password**, not your normal password. For M365, enable SMTP AUTH. |
| `Unsent email saved to processed\outbox` | Fix SMTP, then run `--resend-pending`. The next scheduled run also retries it automatically. |
| `BSE API listing failed` and browser fallback failed | BSE may be down or have changed its site. Re-run later. Try `SCRAPER_MODE=browser`. |
| `No KID PDF found inside the downloaded package` | Open the ZIP from `downloads\` and check the KID's file name. Add a pattern to `KID_FILENAME_PATTERNS`. |
| `Another run appears to be in progress` | A previous run crashed. Delete `processed\.run.lock`. |
| Task result `0x1` / `0x2` | 1 = finished with some failures (see the email's "Items that need attention"); 2 = fatal (see `errors.log`). |
| Task never runs | Check that the PC was on or asleep, wake timers are allowed, and your Windows password hasn't changed. Check the task's History tab. |

### Step 14 — Restart or redeploy after code changes

1. Make sure no run is in progress: `Get-ScheduledTask -TaskName "BSE KID Scraper"` should show `State: Ready`, not `Running`.
2. Replace the files in `src\` and `config\`. **Do not overwrite** `.env`, `processed\` or `.venv\`.
3. If `requirements.txt` changed:
   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
4. Re-test: `.\.venv\Scripts\python.exe -m src.main --check`, then `--dry-run`.
5. The scheduled task needs no change, because it always runs the current code. Re-register it only if you moved the folder.
6. If `.env.example` gained new settings, copy the new lines into your `.env`. Missing settings fall back to safe defaults.

### Step 15 — How duplicate processing is prevented

Three layers:
1. **Per document (download URL).** Once a ZIP is processed successfully, its URL is marked `done` in `processed\state.db` and skipped on every later run, even with a longer lookback window.
2. **Per KID (SHA-256 of the PDF).** If the same KID appears in another ZIP or is re-uploaded, the saved extraction is reused. Claude is not called again and it is not emailed twice.
3. **Per email.** Every KID is marked `emailed_at` once it has been included in an email. A failed email goes to the outbox and is re-sent; it is never rebuilt, so nothing is sent twice.

A run lock prevents two runs overlapping, for example a manual run during the scheduled one.

**Failed documents** are retried automatically on the next runs, up to `MAX_DOC_ATTEMPTS` (3), even if they are no longer in the date window.

To deliberately reprocess:
```powershell
.\.venv\Scripts\python.exe -m src.main --date 29-09-2026 --force          # everything for that date
.\.venv\Scripts\python.exe -m src.main --reset-url "https://www.bseindia.com/corporates/download/12756/PPDIDisclosureDocuments_20260929174501.zip"
```

---

## 3. Late uploads: why `LOOKBACK_DAYS=3` is recommended

The BSE filter is by **allotment date**, but companies often upload their documents **after** allotment.
Here is a real example from BSE: Clean Max Enviro Energy has allotment date **28-09-2026**, but the ZIP was uploaded **29-09-2026 at 17:45**.

- A strict "yesterday only" run on 29-Sep at 08:00 looks at 28-Sep, but the file isn't uploaded yet.
- The run on 30-Sep looks at 29-Sep only, so Clean Max is never checked again.

With `LOOKBACK_DAYS=3`, each run checks the last 3 allotment dates. Anything already processed is skipped instantly, so
**late uploads are caught and nothing is duplicated**. The default is `1` to match the original spec; changing it to `3` is a one-line edit in `.env`.

---

## 4. Customising what Claude extracts

Edit `config\kid_fields.json`. No code change is needed.
- Add, remove or reorder fields. Each field has a `key`, `label`, `type` (`string`/`date`/`list`) and `description`, which tells Claude what to look for.
- `"summary": true` shows the field as a column in the email's summary table. All fields appear in each issue's detail card and in the CSV.

Output is forced through a JSON schema built from this file, so every result has exactly the same keys. Values are also cleaned up afterwards:
- Dates are normalised to `DD-MM-YYYY`.
- Blank or "N/A" values become `Not found`.
- ISIN lists are de-duplicated.

---

## 5. Production features

| Area | Implementation |
|---|---|
| Retries | HTTP: exponential backoff with jitter (`HTTP_MAX_RETRIES`). Claude: `CLAUDE_MAX_RETRIES` on 408/409/429/5xx/529 and connection errors. SMTP: `EMAIL_MAX_RETRIES`. Documents: retried on later runs up to `MAX_DOC_ATTEMPTS`. |
| Timeouts | Separate connect/read timeouts. Long read timeout for large ZIP downloads. Claude request timeout. 3-hour task limit. |
| Rate limits | Minimum interval between Claude calls. Honours the `retry-after` header. Oversized PDFs are trimmed to their first N pages. |
| Duplicate detection | SQLite: URL, KID SHA-256 and emailed-at, plus a run lock. |
| Cleanup and retention | Dated folders are deleted after `RETENTION_DAYS`. Logs rotate daily. The KID archive is kept. |
| Email failure | Retry, then save a `.eml` to the outbox, then auto re-send on the next run or with `--resend-pending`. |
| Safety | Zip-slip protection, ZIP depth and size limits, streamed downloads with size and ZIP-integrity checks, `.part` files for atomic writes. |
| Secrets | `.env` or Windows Credential Manager (`keyring`). Nothing is hard-coded. |
| Configurable | Recipients, schedule time, lookback, model, fields, timeouts and retention are all set in `.env` or `kid_fields.json`. |
| Exit codes | 0 OK · 1 partial · 2 fatal. They are visible in Task Scheduler's "Last Run Result". |

## 6. Command reference

```
python -m src.main                         normal daily run (yesterday / LOOKBACK_DAYS)
python -m src.main --date 29-09-2026       specific allotment date
python -m src.main --from 2026-09-25 --to 2026-09-29
python -m src.main --dry-run               no Claude, no email, nothing marked done
python -m src.main --skip-email            process but don't email
python -m src.main --limit 1               only the first document
python -m src.main --force                 ignore duplicate detection
python -m src.main --check                 validate config + BSE + Claude + SMTP
python -m src.main --test-email
python -m src.main --resend-pending
python -m src.main --status
python -m src.main --reset-url URL
python -m src.main --cleanup
python -m src.main --set-secret ANTHROPIC_API_KEY | SMTP_PASSWORD
python -m src.main --daemon                alternative to Task Scheduler (keeps a process running)
```

## 7. Running the tests (optional)

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -v
```
The tests are fully offline and cover the following:
- Nested ZIPs shaped like real BSE packages, 3-level nesting, zip-slip, corrupt ZIPs and KID detection by content.
- Claude retry on 429.
- Report rendering.
- A full end-to-end run with a local SMTP server, including verifying that a second run sends nothing twice.
