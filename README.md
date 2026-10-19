# ScraperAutomation

One project for **BSE** and **NSE** private-placement debt documents. Each exchange gets its
**own email**:

| Exchange | Page | What is downloaded | Date searched |
|---|---|---|---|
| **BSE** | Markets › Debt › Memorandum of Private Placement Bonds | every listed disclosure ZIP | allotment date |
| **NSE** | Companies › Corporate Filings › Offer Documents › **Debt** tab | only rows with **TYPE = NCD-New Issue** (the link behind the Date column) | filing date |

Then for each exchange: recursive unzip, find KID PDFs, optional Claude extraction, and **that exchange's own email** with its KIDs zipped.

This single project replaces the older `bse-kid-scraper` and `ExchangeScrapper` folders.

---

## How it fits together (in .NET terms)

```
src\main.py            Program.Main - reads options, runs each exchange in turn
src\pipeline.py        the per-exchange workflow (one run = one exchange = one email)
src\exchanges\         one class per exchange, all with the same interface
    bse.py             BSEScraper  - also the base class (browser, downloads, file-lock handling)
    nse.py             NSEScraper  - inherits BSEScraper, overrides listing + page details
    __init__.py        registry: EXCHANGES = {"BSE": ..., "NSE": ...}
src\zip_processor.py   recursive unzip + KID detection
src\email_service.py   report + SMTP (subject/recipients per exchange)
src\config.py          settings from .env; HARDCODED_DATE at the top
src\state.py           SQLite: what was processed/emailed (processed\state.db)
```

- **Adding another exchange later** means one new file in `src\exchanges\` plus one line in the registry.
- **If NSE fails** (site down or blocked), BSE still runs and sends its email, and the reverse is also true. The exit code is the worst of the runs: 0 = ok, 1 = some issues, 2 = fatal.

### How NSE is read (verified on the live site)

The Debt tab calls
`https://www.nseindia.com/api/corporates/offerdocs?index=debt&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY`,
which returns one JSON item per table row. The `documentType` field is the TYPE column, and `attachFile` is the link behind the date.

- **Rows used:** only `NCD-New Issue` rows are kept (spaces and case are ignored). The same file listed twice is downloaded once.
- **Blocking:** NSE blocks plain scripts, so the program uses the same approach as BSE. It tries the direct API first; if that's blocked, it uses a hidden Edge window that opens the page, clicks the **Debt** tab and reads the data.
- **What's in an NSE file:** each one is a ZIP with **one big PDF**. Sometimes it's "GID and KID" merged, which is found by its name. Sometimes it's **only a GID**; then the email lists it under "Items that need attention" with the file name, instead of pretending it found a KID.

---

## Output folders

```
D:\ScraperAutomationLogs\                 (LOGS_DIR and OUTPUT_DIR in .env)
   scraper_automation.log, errors.log     every line is tagged exch.bse... / exch.nse...
   2026_09_17\                           the date searched
      BSE\
         <Company>_<ISIN>\               the downloaded ZIP + unzipped\
         KidFiles\                       every KID found
         KidFilesZip_2026_09_17.zip      what the BSE email carries
         report_*.html  results_*.csv/.json  email_*.eml
      NSE\                               same layout, NSE only
```

- **Re-running a date** resets only that exchange's folder.
- **Stuck files:** if Windows won't let old files be deleted (antivirus or an open window is holding them), the run uses a fresh folder such as `NSE_143502` instead of failing. Your old BSE logs had this problem (`Access is denied`), and this fixes it.
- **Retention:** day folders untouched for `OUTPUT_RETENTION_DAYS` (3) days are deleted automatically.

---

## Setup (Command Prompt)

```cmd
cd /d D:\Projects\ScraperAutomation
scripts\setup.bat
```

Then create your settings file (your "appsettings"):

```cmd
copy .env.example .env
notepad .env
```

Fill in the SMTP block once (shared by both exchanges), then each exchange's own email block:

```ini
# ---- BSE email ----
BSE_EMAIL_TO=person1@akgroup.co.in, person2@akgroup.co.in
BSE_EMAIL_CC=
BSE_EMAIL_FROM=UATSecurityService@akgroup.co.in
BSE_EMAIL_SUBJECT_PREFIX=BSE Private Placement KIDs
# ---- NSE email ----
NSE_EMAIL_TO=person3@akgroup.co.in
NSE_EMAIL_CC=
NSE_EMAIL_FROM=UATSecurityService@akgroup.co.in
NSE_EMAIL_SUBJECT_PREFIX=NSE NCD New Issue KIDs
```
Any blank BSE_/NSE_ value falls back to the shared `EMAIL_TO` / `EMAIL_CC` / `EMAIL_BCC` / `EMAIL_FROM`.
`.env.example` lists every setting with explanations.

**The date** is set at the top of `src\config.py`: `HARDCODED_DATE = "17/09/2026"`. Set it to `""` for the daily run (yesterday).

---

## Test, in this order

| # | Command | Expect |
|---|---|---|
| 1 | `.venv\Scripts\python.exe -m src.main --exchange NSE --dry-run --date 17-09-2026` | `Documents found: 5 NCD-New Issue row(s) → 4 unique download(s)` and files in `...\2026_09_17\NSE\` |
| 2 | `.venv\Scripts\python.exe -m src.main --exchange BSE --dry-run --date 17-09-2026` | BSE as before, in `...\2026_09_17\BSE\` |
| 3 | `.venv\Scripts\python.exe -m src.main --exchange ALL --skip-email --date 17-09-2026` | both, no emails |
| 4 | `.venv\Scripts\python.exe -m src.main --exchange ALL --date 17-09-2026` | **two emails**: "BSE Private Placement KIDs" and "NSE NCD New Issue KIDs" |
| 5 | `.venv\Scripts\python.exe -m src.main --status` | runs and documents, tagged [BSE]/[NSE] |

Notes:
- Step 1 downloads 4 files of roughly 20–48 MB each, so it takes a few minutes.
- In VS Code, F5 offers the same steps as profiles: "NSE dry run - date", "ALL dry run - date" and so on.
- Add `BROWSER_HEADLESS=false` to `.env` to watch Edge do the NSE steps.

Offline tests: `.venv\Scripts\python.exe -m pip install -r requirements-dev.txt`, then `.venv\Scripts\python.exe -m pytest`.

## Daily schedule

Open Command Prompt **as Administrator**:
```cmd
cd /d D:\Projects\ScraperAutomation
scripts\register_task.bat 08:00
```
It also removes the older "ExchangeScrapper" / "BSE KID Scraper" tasks, so nothing is emailed twice. Before enabling the schedule, set `HARDCODED_DATE = ""` in `src\config.py`.

## Other commands

```
--exchange BSE|NSE|ALL   which exchange(s); default = EXCHANGES in .env
--date 17-09-2026        one date        --from / --to   a range
--dry-run                no Claude, no email      --skip-email   everything but the email
--check                  tests BSE, NSE, Claude, SMTP
--test-email / --diagnose-smtp / --resend-pending / --status / --cleanup
```
