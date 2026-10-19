"""
BSE "Memorandum of Private Placement Bonds" scraper.

How the page works (verified against the live site):
  * https://www.bseindia.com/markets/debt/memorandum_data is an Angular app.
  * When you pick From/To dates and click Submit, the page calls
        https://api.bseindia.com/BseIndiaAPI/api/Debtmemorandumbetadata/w
            ?txtname=&ddldate=1&fdate=YYYY-MM-DD&tdate=YYYY-MM-DD
    (ddldate=1 -> "Allotment Date", 2 -> "Redemption Date")
    and renders the JSON {"Table": [...]} as the table. An empty result is "{}".
  * Each row has FILE1 = "/corporates/download/<id>/PPDIDisclosureDocuments_<ts>.zip".
    Several ISINs of the same issuer usually share ONE zip.

Modes (SCRAPER_MODE):
  api     - call the JSON endpoint directly (fast, no browser).
  browser - drive a real Chromium with Playwright: open page, type dates, click
            Submit, capture the table data (exactly what a human would do).
  auto    - api first; if it fails, fall back to browser.
"""
from __future__ import annotations

import logging
import re
import shutil
import time
import zipfile
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote, urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter

from ..config import Settings
from ..utils import retry_call, safe_name, who_is_locking

log = logging.getLogger("exch.bse")

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
}


class BSEError(Exception):
    pass


class BSEBlockedError(BSEError):
    """BSE refused a plain HTTP request (401/403). Retrying won't help; use the browser."""


@dataclass
class Issue:
    """One row of the BSE table."""
    company: str
    isin: str
    allotment_date: Optional[date]
    redemption_date: Optional[date]
    file_url: str


@dataclass
class Document:
    """One downloadable package (a zip may cover several ISINs)."""
    url: str
    company: str
    isins: List[str] = field(default_factory=list)
    allotment_dates: List[str] = field(default_factory=list)
    redemption_dates: List[str] = field(default_factory=list)
    local_path: Optional[Path] = None
    exchange: str = "BSE"
    doc_types: List[str] = field(default_factory=list)      # NSE: TYPE column, e.g. "NCD-New Issue"
    particulars: List[str] = field(default_factory=list)    # NSE: "Particulars" column

    @property
    def doc_id(self) -> str:
        # "/corporates/download/13220/PPDI..." -> "13220"
        m = re.search(r"/download/(\d+)/", self.url)
        return m.group(1) if m else (Path(urlsplit(self.url).path).stem or "x")

    @property
    def file_name(self) -> str:
        return Path(urlsplit(self.url).path).name

    @property
    def folder_name(self) -> str:
        """'<Company>_<ISIN>' (plus '_+N' when the package covers more ISINs).

        Names the document's folder and its downloaded file."""
        company = (self.company or "Unknown").strip()[:80]
        isin = self.isins[0] if self.isins else self.doc_id
        extra = f"_+{len(self.isins) - 1}" if len(self.isins) > 1 else ""
        return safe_name(f"{company}_{isin}{extra}")


def _parse_bse_date(value) -> Optional[date]:
    if not value:
        return None
    s = str(value)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(s[:19] if "T" in s else s, fmt).date()
        except ValueError:
            continue
    return None


class BSEScraper:
    """BSE scraper, and the base class for other exchanges (see exchanges/nse.py).

    Subclasses override EXCHANGE, READY_SELECTOR, page_url and fetch_documents(); the browser,
    download, retry and file-lock handling below is shared."""
    EXCHANGE = "BSE"
    READY_SELECTOR = "#btnSubmit"          # element that proves the page has loaded

    @property
    def page_url(self) -> str:
        return self.page_url

    def __init__(self, settings: Settings):
        self.s = settings
        self.log = logging.getLogger(f"exch.{self.EXCHANGE.lower()}")
        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=4)
        self.session.mount("https://", adapter)
        self._warmed_up = False
        # Browser state (created lazily, reused for listing + downloads, closed by close())
        self._pw = None
        self._channel = ""
        self._form_ready = False
        self._browser = None
        self._ctx = None
        self._page = None
        self._use_browser_downloads = settings.scraper_mode == "browser"

    def close(self) -> None:
        for obj, meth in ((self._ctx, "close"), (self._browser, "close"), (self._pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, meth)()
            except Exception:
                pass
        self._pw = self._browser = self._ctx = self._page = None

    # ================================================================ listing
    def fetch_documents(self, date_from: date, date_to: date) -> List[Document]:
        """Return unique downloadable documents whose allotment date is in range."""
        self.log.info("Fetching BSE listing for %s → %s (mode=%s)", date_from, date_to, self.s.scraper_mode)
        issues: Optional[List[Issue]] = None
        errors = []

        if self.s.scraper_mode in ("api", "auto"):
            try:
                issues = self._fetch_via_api(date_from, date_to)
            except Exception as exc:
                errors.append(f"api: {exc}")
                self.log.warning("API listing failed: %s", exc)
                if self.s.scraper_mode == "auto":
                    self.log.info("Switching to real-browser mode (listing and downloads)")
                    self._use_browser_downloads = True
                if self.s.scraper_mode == "api":
                    raise BSEError(f"BSE API listing failed: {exc}") from exc

        if issues is None and self.s.scraper_mode in ("browser", "auto"):
            try:
                issues = self._fetch_via_browser(date_from, date_to)
            except Exception as exc:
                errors.append(f"browser: {exc}")
                self.log.error("Browser listing failed: %s", exc)

        if issues is None:
            raise BSEError("Could not fetch BSE listing — " + "; ".join(errors))

        # Defensive: keep only rows whose allotment date is inside the range
        if self.s.bse_date_type == "1":
            before = len(issues)
            issues = [i for i in issues if i.allotment_date is None or date_from <= i.allotment_date <= date_to]
            if len(issues) != before:
                self.log.info("Filtered %d rows outside the date range", before - len(issues))

        docs = self._group(issues)
        self.log.info("Documents found: %d issue row(s) → %d unique download(s)", len(issues), len(docs))
        for d in docs:
            self.log.info("  • %s | ISIN(s): %s | %s", d.company, ", ".join(d.isins), d.url)
        return docs

    def _warm_up(self) -> None:
        """Visit the page once to pick up cookies, like a real browser would."""
        if self._warmed_up:
            return
        try:
            r = self.session.get(
                self.page_url,
                timeout=(self.s.http_connect_timeout, self.s.http_read_timeout),
                headers={"Accept": "text/html,application/xhtml+xml"},
            )
            self.log.info("%s page access: HTTP %s (%s)", self.EXCHANGE, r.status_code, self.page_url)
        except requests.RequestException as exc:
            self.log.warning("%s page warm-up failed (continuing): %s", self.EXCHANGE, exc)
        self._warmed_up = True

    def _fetch_via_api(self, date_from: date, date_to: date) -> List[Issue]:
        self._warm_up()
        params = {
            "txtname": "",
            "ddldate": self.s.bse_date_type,
            "fdate": date_from.strftime("%Y-%m-%d"),  # same format the BSE page sends
            "tdate": date_to.strftime("%Y-%m-%d"),
        }
        self.log.info("Date selection: From=%s To=%s (date type=%s) — submitting query",
                 date_from.strftime("%d/%m/%Y"), date_to.strftime("%d/%m/%Y"),
                 "Allotment Date" if self.s.bse_date_type == "1" else "Redemption Date")

        def call() -> dict:
            r = self.session.get(
                self.s.bse_api_url, params=params,
                timeout=(self.s.http_connect_timeout, self.s.http_read_timeout),
            )
            if r.status_code in (401, 403):
                raise BSEBlockedError(f"BSE refused the direct API request (HTTP {r.status_code})")
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
            r.raise_for_status()
            ctype = r.headers.get("content-type", "")
            if "json" not in ctype and not r.text.strip().startswith("{"):
                raise BSEError(f"Unexpected response type {ctype!r}: {r.text[:200]!r}")
            return r.json()

        data = retry_call(
            call, attempts=self.s.http_max_retries,
            retry_on=(requests.RequestException, ValueError),
            what="BSE listing API", logger=self.log,
        )
        issues = self.parse_api_payload(data, self.s.bse_base_url)
        self.log.info("BSE API returned %d row(s)", len(issues))
        return issues

    @staticmethod
    def parse_api_payload(data: dict, base_url: str) -> List[Issue]:
        rows = (data or {}).get("Table") or []
        issues: List[Issue] = []
        for row in rows:
            file_url = (row.get("FILE1") or row.get("file1") or "").strip()
            if not file_url:
                log.warning("Row without download link skipped: %s", row.get("name"))
                continue
            if row.get("IsNotAvailable") in (1, "1", True):
                log.warning("Row marked not available, skipped: %s", row.get("name"))
                continue
            abs_url = urljoin(base_url + "/", file_url.lstrip("/"))
            issues.append(Issue(
                company=(row.get("name") or "").strip(),
                isin=(row.get("ISIN") or "").strip(),
                allotment_date=_parse_bse_date(row.get("date1")),
                redemption_date=_parse_bse_date(row.get("date2")),
                file_url=abs_url,
            ))
        return issues

    @staticmethod
    def _group(issues: List[Issue]) -> List[Document]:
        grouped: "OrderedDict[str, Document]" = OrderedDict()
        for i in issues:
            d = grouped.get(i.file_url)
            if d is None:
                d = grouped[i.file_url] = Document(url=i.file_url, company=i.company)
            if i.isin and i.isin not in d.isins:
                d.isins.append(i.isin)
            ad = i.allotment_date.strftime("%d-%m-%Y") if i.allotment_date else ""
            rd = i.redemption_date.strftime("%d-%m-%Y") if i.redemption_date else ""
            if ad and ad not in d.allotment_dates:
                d.allotment_dates.append(ad)
            if rd and rd not in d.redemption_dates:
                d.redemption_dates.append(rd)
        return list(grouped.values())

    # ---------------------------------------------------------------- browser
    def _ensure_browser(self):
        """Start a real browser once. Prefers Microsoft Edge / Google Chrome already installed
        on the PC (they use the Windows certificate store, so they work behind corporate
        SSL inspection and need no download), then Playwright's own Chromium."""
        if self._page is not None:
            return self._page
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BSEError("Playwright not installed (pip install -r requirements.txt)") from exc

        self._pw = sync_playwright().start()
        channels = [c.strip() for c in self.s.browser_channels.split(",") if c.strip()]
        last_exc: Optional[Exception] = None
        # Hide the usual automation give-aways (sites with bot protection block them)
        args = ["--disable-blink-features=AutomationControlled", "--no-first-run",
                "--no-default-browser-check", "--disable-extensions"]
        for ch in channels:
            try:
                kwargs = {"headless": self.s.browser_headless, "args": args, "timeout": 60_000}
                if ch != "chromium":
                    kwargs["channel"] = ch
                self._browser = self._pw.chromium.launch(**kwargs)
                self.log.info("Browser started: %s %s (headless=%s)", ch, self._browser.version, self.s.browser_headless)
                self._channel = ch
                break
            except Exception as exc:
                last_exc = exc
                self.log.info("Browser '%s' not available: %s", ch, str(exc).splitlines()[0])
        if self._browser is None:
            self.close()
            raise BSEError(f"No usable browser (tried {channels}). Last error: {last_exc}")

        # A normal desktop user-agent: headless mode otherwise announces "HeadlessChrome".
        ver = self._browser.version or "140.0.0.0"
        ua = (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              f"Chrome/{ver} Safari/537.36")
        if self._channel == "msedge":
            ua += f" Edg/{ver}"
        self._ctx = self._browser.new_context(
            accept_downloads=True, user_agent=ua, locale="en-IN", timezone_id="Asia/Kolkata",
            viewport={"width": 1366, "height": 850},
            extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"},
        )
        self._ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        self._page = self._ctx.new_page()
        self._page.set_default_timeout(self.s.http_read_timeout * 1000)
        self.log.info("Browser mode: opening %s", self.page_url)
        resp = self._page.goto(self.page_url, wait_until="domcontentloaded", timeout=90_000)
        self.log.info("%s page response: HTTP %s", self.EXCHANGE, resp.status if resp else "?")
        try:
            self._page.wait_for_selector(self.READY_SELECTOR, timeout=45_000)
            self.log.info("%s page access: loaded in browser", self.EXCHANGE)
            self._form_ready = True
        except Exception:
            self._form_ready = False
            self._save_diagnostics("page_not_ready")
            self.log.warning("%s page did not finish loading (title=%r). Will query the data from inside the page instead.", self.EXCHANGE,
                        self._safe_title())
        return self._page

    def _safe_title(self) -> str:
        try:
            return self._page.title()
        except Exception:
            return "?"

    def _save_diagnostics(self, tag: str) -> None:
        """Screenshot + HTML of what the browser actually sees, for troubleshooting."""
        try:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            base = self.s.logs_dir / f"browser_{tag}_{stamp}"
            self._page.screenshot(path=str(base.with_suffix(".png")), full_page=False)
            base.with_suffix(".html").write_text(self._page.content()[:500_000], encoding="utf-8")
            self.log.warning("Saved what the browser saw: %s.png / .html", base)
        except Exception as exc:
            self.log.debug("Could not save diagnostics: %s", exc)

    def _fetch_via_browser(self, date_from: date, date_to: date) -> List[Issue]:
        """Human-like flow: open page → select date type → type From/To → click Submit → read results."""
        page = self._ensure_browser()
        f_str, t_str = date_from.strftime("%d/%m/%Y"), date_to.strftime("%d/%m/%Y")
        data = None
        try:
            if not getattr(self, "_form_ready", False):
                raise BSEError("form not available")
            page.select_option("#DateType", self.s.bse_date_type)
            for sel, val in (("#datepicker_1", f_str), ("#datepicker_2", t_str)):
                page.click(sel)
                page.fill(sel, "")
                page.type(sel, val, delay=30)
                page.keyboard.press("Escape")  # close the calendar popup
            self.log.info("Date selection: From=%s To=%s — clicking Submit", f_str, t_str)
            with page.expect_response(lambda r: "Debtmemorandumbetadata" in r.url and "fdate=" in r.url
                                      and r.request.method == "GET", timeout=60_000) as resp_info:
                page.click("#btnSubmit")
            resp = resp_info.value
            self.log.info("Submit → BSE returned HTTP %s", resp.status)
            data = resp.json() if resp.ok else None
        except Exception as exc:
            self.log.warning("Form submit flow failed (%s) — querying from inside the page instead", exc)

        if data is None:
            self.log.info("Querying BSE listing from inside the browser page")
            data = page.evaluate(
                """async (u) => { const r = await fetch(u, {credentials: 'include'});
                                  if (!r.ok) throw new Error('HTTP ' + r.status); return await r.json(); }""",
                f"{self.s.bse_api_url}?txtname=&ddldate={self.s.bse_date_type}"
                f"&fdate={date_from:%Y-%m-%d}&tdate={date_to:%Y-%m-%d}",
            )
        issues = self.parse_api_payload(data, self.s.bse_base_url)
        self.log.info("Browser listing returned %d row(s)", len(issues))
        return issues

    def _download_via_browser(self, url: str, part: Path) -> None:
        """Let the real browser download the file (same as clicking the download icon)."""
        page = self._ensure_browser()
        with page.expect_download(timeout=120_000) as dl_info:
            page.evaluate(
                """(u) => { const a = document.createElement('a'); a.href = u; a.download = '';
                            document.body.appendChild(a); a.click(); a.remove(); }""", url)
        dl = dl_info.value
        failure = dl.failure()  # waits for the download to finish
        if failure:
            raise requests.ConnectionError(f"Browser download failed: {failure}")
        dl.save_as(str(part))

    # =============================================================== download
    def download(self, doc: Document, target_dir: Path, name: Optional[str] = None) -> Path:
        """Stream the file to disk with retries, size checks and ZIP validation.

        `name` (without extension) renames the saved file; default keeps the BSE name."""
        target_dir.mkdir(parents=True, exist_ok=True)
        if name:
            dest = target_dir / safe_name(name + (Path(doc.file_name).suffix or ".zip"))
        else:
            dest = target_dir / safe_name(f"{doc.doc_id}_{doc.file_name}")
        if dest.exists() and dest.stat().st_size > 0 and (
            not dest.suffix.lower() == ".zip" or zipfile.is_zipfile(dest)
        ):
            self.log.info("Download skipped, already on disk: %s", dest.name)
            doc.local_path = dest
            return dest

        part = dest.with_suffix(dest.suffix + ".part")
        # BSE URLs sometimes contain spaces
        parts = urlsplit(doc.url)
        url = parts._replace(path=quote(parts.path, safe="/%")).geturl()
        max_bytes = self.s.max_download_mb * 1024 * 1024

        def attempt() -> Path:
            start = time.time()
            if self._use_browser_downloads:
                self._download_via_browser(url, part)
                if part.stat().st_size > max_bytes:
                    raise BSEError("Download exceeded MAX_DOWNLOAD_MB")
                return finish(start)
            with self.session.get(
                url, stream=True,
                timeout=(self.s.http_connect_timeout, self.s.download_read_timeout),
                headers={"Accept": "*/*", "Referer": self.page_url},
            ) as r:
                if r.status_code in (401, 403) and self.s.scraper_mode != "api":
                    self.log.warning("%s refused the direct download (HTTP %s) — using the browser", self.EXCHANGE, r.status_code)
                    self._use_browser_downloads = True
                    raise requests.ConnectionError("switching to browser download")
                if r.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
                r.raise_for_status()
                expected = int(r.headers.get("content-length") or 0)
                if expected and expected > max_bytes:
                    raise BSEError(f"File too large ({expected/1e6:.0f} MB > MAX_DOWNLOAD_MB)")
                written = 0
                with open(part, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            fh.write(chunk)
                            written += len(chunk)
                            if written > max_bytes:
                                raise BSEError("Download exceeded MAX_DOWNLOAD_MB")
                if expected and written != expected:
                    raise requests.ConnectionError(f"Incomplete download: {written} of {expected} bytes")
            return finish(start)

        def finish(start: float) -> Path:
            if dest.suffix.lower() == ".zip" and not zipfile.is_zipfile(part):
                head = part.read_bytes()[:200]
                raise requests.ConnectionError(f"Downloaded file is not a valid ZIP (starts with {head[:60]!r})")
            # Antivirus / Windows Search often hold a just-written file for a moment
            # (WinError 5 "Access is denied"); wait and retry instead of failing the package.
            for wait in (1, 2, 3, 5, 10, 0):
                try:
                    part.replace(dest)
                    break
                except PermissionError:
                    holder = who_is_locking(part) or "unknown program"
                    if wait:
                        self.log.warning("%s is locked by %s; retrying in %ds", part.name, holder, wait)
                        time.sleep(wait)
                        continue
                    # Still locked: a reader (e.g. a virus scan) blocks renaming but not
                    # copying, so copy the finished download instead of failing the package.
                    self.log.warning("%s is still locked by %s; copying it instead of renaming", part.name, holder)
                    shutil.copyfile(part, dest)
                    try:
                        part.unlink()
                    except OSError:
                        self.log.warning("Could not remove %s (locked by %s); it can be deleted later",
                                    part.name, who_is_locking(part) or "unknown program")
            self.log.info("Downloaded %s (%.1f MB in %.0fs)", dest.name, dest.stat().st_size / 1e6, time.time() - start)
            return dest

        try:
            path = retry_call(attempt, attempts=self.s.http_max_retries,
                              retry_on=(requests.RequestException,), what=f"Download {doc.file_name}",
                              base_delay=5, logger=self.log)
        finally:
            try:
                part.unlink(missing_ok=True)
            except OSError:   # still locked; harmless leftover, the run carries on
                pass
        doc.local_path = path
        return path

    def check_connectivity(self) -> Dict[str, str]:
        """Used by --check: verify the page and API respond."""
        out = {}
        try:
            r = self.session.get(self.page_url, timeout=(15, 30))
            out["page"] = f"HTTP {r.status_code}"
        except Exception as exc:
            out["page"] = f"ERROR {exc}"
        try:
            r = self.session.get(self.s.bse_api_url, params={"txtname": "", "ddldate": "1",
                                 "fdate": "2026-01-01", "tdate": "2026-01-01"}, timeout=(15, 60))
            out["api"] = f"HTTP {r.status_code}, {len(r.content)} bytes"
        except Exception as exc:
            out["api"] = f"ERROR {exc}"
        return out
