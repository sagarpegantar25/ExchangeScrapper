"""
Central configuration.

All settings come from environment variables (normally loaded from the `.env`
file in the project root). Secrets (Claude API key, SMTP password) can instead
be stored in Windows Credential Manager via `python -m src.main --set-secret NAME`;
an environment variable always takes precedence over the credential store.

Nothing secret is ever hard-coded in source.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
KEYRING_SERVICE = "scraper-automation"
# Older names this project had; secrets saved under them keep working.
LEGACY_KEYRING_SERVICES = ("exchange-scrapper", "bse-kid-scraper")

# ---- HARD-CODED RUN DATE -------------------------------------------------------
# The date searched on BSE (allotment date) and NSE (filing date), e.g. "17/09/2026" (also 17-09-2026, 17-sep-2026).
# Leave it "" (empty) for the normal daily behaviour: process YESTERDAY's data.
# (--date on the command line still wins, for one-off runs.)
HARDCODED_DATE = "17/09/2026"
# --------------------------------------------------------------------------------

# Accepted spellings of a date: 28-09-2026, 28/09/2026, 2026-09-28, 28-sep-2026, 28-September-2026
DATE_INPUT_FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y", "%d %b %Y")

# Name of each day's folder under OUTPUT_DIR: the date searched on BSE, e.g. logs\2026_09_28
DAY_FOLDER_FORMAT = "%Y_%m_%d"

# Load .env once, without overriding variables already set in the real environment.
load_dotenv(PROJECT_ROOT / ".env", override=False)


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


# ---------------------------------------------------------------- helpers
def _get(name: str, default: Optional[str] = None) -> Optional[str]:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    return val.strip()


def _get_int(name: str, default: int) -> int:
    raw = _get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _get_float(name: str, default: float) -> float:
    raw = _get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _get_bool(name: str, default: bool) -> bool:
    raw = _get(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on"}


def parse_date(raw: str, name: str) -> Optional[date]:
    """Parse a date in any of DATE_INPUT_FORMATS; blank -> None."""
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in DATE_INPUT_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    raise ConfigError(f"{name} must be a date like 29-09-2026, got {raw!r}")


def _get_list(name: str) -> List[str]:
    raw = _get(name, "") or ""
    parts = [p.strip() for p in raw.replace(";", ",").split(",")]
    return [p for p in parts if p]


def _path(name: str, default: str) -> Path:
    p = Path(_get(name, default))
    return p if p.is_absolute() else (PROJECT_ROOT / p)


def get_secret(name: str) -> Optional[str]:
    """Environment variable first, then Windows Credential Manager (keyring)."""
    val = _get(name)
    if val:
        return val
    try:
        import keyring  # optional dependency

        # Also look under the old project's name, so secrets saved for bse-kid-scraper keep working.
        for service in (KEYRING_SERVICE, *LEGACY_KEYRING_SERVICES):
            val = keyring.get_password(service, name)
            if val:
                return val
        return None
    except Exception:
        return None


def set_secret(name: str, value: str) -> None:
    import keyring

    keyring.set_password(KEYRING_SERVICE, name, value)


# ---------------------------------------------------------------- settings
@dataclass
class Settings:
    # --- general
    timezone: str = "Asia/Kolkata"
    environment: str = "production"

    # --- which exchanges to run (EXCHANGES=BSE,NSE); --exchange on the command line overrides
    exchanges: List[str] = field(default_factory=lambda: ["BSE", "NSE"])
    exchange: str = "BSE"   # the exchange a pipeline run is for (set by for_exchange())

    # --- NSE  (Corporate filings > Offer documents > Debt tab)
    nse_page_url: str = "https://www.nseindia.com/companies-listing/corporate-filings-offer-documents"
    nse_api_url: str = "https://www.nseindia.com/api/corporates/offerdocs"
    nse_base_url: str = "https://www.nseindia.com"
    nse_index: str = "debt"
    nse_document_types: List[str] = field(default_factory=lambda: ["NCD-New Issue"])

    # --- BSE
    bse_page_url: str = "https://www.bseindia.com/markets/debt/memorandum_data"
    bse_api_url: str = "https://api.bseindia.com/BseIndiaAPI/api/Debtmemorandumbetadata/w"
    bse_base_url: str = "https://www.bseindia.com"
    bse_date_type: str = "1"  # 1 = Allotment Date, 2 = Redemption Date (as on the page)
    lookback_days: int = 1  # 1 = yesterday only
    fixed_date: Optional[date] = None  # from HARDCODED_DATE; None = yesterday
    scraper_mode: str = "auto"  # auto | api | browser
    browser_channels: str = "msedge,chrome,chromium"
    browser_headless: bool = True
    use_windows_cert_store: bool = True
    http_connect_timeout: int = 30
    http_read_timeout: int = 120
    download_read_timeout: int = 600
    http_max_retries: int = 4
    max_download_mb: int = 1024

    # --- ZIP processing
    max_zip_depth: int = 8
    max_extracted_mb: int = 4096
    kid_filename_patterns: List[str] = field(default_factory=list)
    kid_content_fallback: bool = True

    # --- Claude
    claude_enabled: bool = True  # false = skip extraction; just email the KID PDFs
    anthropic_api_key: Optional[str] = None
    claude_model: str = "claude-sonnet-5-5"
    claude_max_tokens: int = 4096
    claude_timeout: int = 300
    claude_max_retries: int = 5
    claude_min_interval_sec: float = 3.0
    claude_max_pdf_pages: int = 100
    claude_max_pdf_mb: float = 20.0
    fields_file: Path = PROJECT_ROOT / "config" / "kid_fields.json"

    # --- Email
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_security: str = "starttls"  # starttls | ssl | none
    smtp_auth_method: str = ""       # blank = automatic; or LOGIN / PLAIN / CRAM-MD5
    smtp_username: str = ""
    smtp_password: Optional[str] = None
    email_from: str = ""
    email_from_name: str = "KID Bot"
    email_to: List[str] = field(default_factory=list)
    email_cc: List[str] = field(default_factory=list)
    email_bcc: List[str] = field(default_factory=list)
    email_subject_prefix: str = "BSE Private Placement KIDs"
    email_send_when_empty: bool = True
    email_attach_csv: bool = True
    email_attach_kids: bool = True          # attach the KID PDFs themselves
    email_max_attachments_mb: float = 20.0  # total cap; KIDs beyond it are listed, not attached
    email_max_retries: int = 3
    # per-exchange email settings: BSE_EMAIL_TO / NSE_EMAIL_TO etc. (blank = the shared EMAIL_* value)
    exchange_email: Dict[str, Dict[str, object]] = field(default_factory=dict)

    # --- Schedule (used by --daemon mode and the Task Scheduler helper)
    schedule_time: str = "08:00"

    # --- Paths & retention
    downloads_dir: Path = PROJECT_ROOT / "downloads"
    extracted_dir: Path = PROJECT_ROOT / "extracted"
    processed_dir: Path = PROJECT_ROOT / "processed"
    logs_dir: Path = PROJECT_ROOT / "logs"
    retention_days: int = 30          # downloads/ and extracted/
    output_dir: Optional[Path] = None   # e.g. logs -> day-wise folders logs\\2026_10_01\\
    output_retention_days: int = 3
    kid_archive_retention_days: int = 0  # 0 = keep KID archive forever
    log_retention_days: int = 90
    log_level: str = "INFO"
    max_doc_attempts: int = 3
    # false = every run re-processes the date's documents and emails all their KIDs again
    # (a rerun resends the email). true = skip documents/KIDs that were already emailed.
    skip_already_emailed: bool = False

    # ------------------------------------------------------------ derived
    @property
    def state_db(self) -> Path:
        return self.processed_dir / "state.db"

    @property
    def kid_archive_dir(self) -> Path:
        return self.processed_dir / "kids"

    @property
    def reports_dir(self) -> Path:
        return self.processed_dir / "reports"

    @property
    def outbox_dir(self) -> Path:
        return self.processed_dir / "outbox"

    @property
    def lock_file(self) -> Path:
        return self.processed_dir / ".run.lock"

    @property
    def output_root(self) -> Path:
        """Parent of the day folders: OUTPUT_DIR (e.g. logs), else processed\\output."""
        return self.output_dir or (self.processed_dir / "output")

    def day_dir(self, day: date) -> Path:
        """One folder per date searched; each exchange has its own sub-folder (BSE\\, NSE\\):

          logs\\2026_09_17\\
            BSE\\
              <Company>_<ISIN>\\           one per downloaded package
                <Company>_<ISIN>.zip     package downloaded from BSE
                unzipped\\               everything extracted from it (nested zips too)
              KidFiles\\                 every KID found, named <Company>_<ISIN>_<file>.pdf
              KidFilesZip_2026_09_17.zip all the KIDs zipped (the complete set)
              KidFilesZip_2026_09_17_part1.zip, _part2.zip...  when too big for one email:
                                         one email per part, each listing its own files
              KidFilesSplit\\            a KID too big for any email, split by pages
                                         (<name>_pages1-187.pdf ...) so it can still be sent
            report_*.html, results_*.json, email_*.eml
        """
        return self.output_root / day.strftime(DAY_FOLDER_FORMAT)

    def for_exchange(self, code: str) -> "Settings":
        """A copy of the settings for one exchange: its own email recipients/subject/sender name.

        Like creating a per-tenant options object in .NET - everything else is shared."""
        code = code.upper()
        e = self.exchange_email.get(code, {})
        return replace(
            self, exchange=code,
            email_to=list(e.get("to") or self.email_to),
            email_cc=list(e.get("cc") or self.email_cc),
            email_bcc=list(e.get("bcc") or self.email_bcc),
            email_subject_prefix=str(e.get("subject") or DEFAULT_SUBJECTS.get(code, self.email_subject_prefix)),
            email_from=str(e.get("from") or self.email_from),
            email_from_name=str(e.get("from_name") or DEFAULT_FROM_NAMES.get(code, self.email_from_name)),
        )

    def ensure_dirs(self) -> None:
        for d in (self.downloads_dir, self.extracted_dir, self.processed_dir, self.logs_dir,
                  self.kid_archive_dir, self.reports_dir, self.outbox_dir):
            d.mkdir(parents=True, exist_ok=True)
        if self.output_dir:
            self.output_dir.mkdir(parents=True, exist_ok=True)

    def load_fields(self) -> list:
        with open(self.fields_file, encoding="utf-8") as fh:
            data = json.load(fh)
        fields = data["fields"] if isinstance(data, dict) else data
        if not fields:
            raise ConfigError(f"No fields defined in {self.fields_file}")
        return fields

    # ------------------------------------------------------------ validation
    def validate(self, need_claude: bool = True, need_email: bool = True) -> List[str]:
        """Return a list of problems (empty list = OK)."""
        problems: List[str] = []
        if need_claude and self.claude_enabled and not self.anthropic_api_key:
            problems.append("ANTHROPIC_API_KEY is not set (.env or --set-secret ANTHROPIC_API_KEY).")
        if need_email:
            if not self.smtp_host:
                problems.append("SMTP_HOST is not set.")
            for code in self.exchanges:      # every exchange needs its own (or the shared) To + From
                xs = self.for_exchange(code)
                if not xs.email_to:
                    problems.append(f"{code}_EMAIL_TO (or the shared EMAIL_TO) has no recipients.")
                if not xs.email_from:
                    problems.append(f"{code}_EMAIL_FROM (or the shared EMAIL_FROM) is not set.")
            if self.smtp_username and not self.smtp_password:
                problems.append("SMTP_PASSWORD is not set (.env or --set-secret SMTP_PASSWORD).")
            if self.smtp_security not in {"starttls", "ssl", "none"}:
                problems.append("SMTP_SECURITY must be starttls, ssl or none.")
        bad = [x for x in self.exchanges if x not in ("BSE", "NSE")]
        if bad:
            problems.append(f"EXCHANGES contains unknown exchange(s): {', '.join(bad)} (use BSE, NSE).")
        if self.scraper_mode not in {"auto", "api", "browser"}:
            problems.append("SCRAPER_MODE must be auto, api or browser.")
        if self.lookback_days < 1:
            problems.append("LOOKBACK_DAYS must be >= 1.")
        try:
            hh, mm = self.schedule_time.split(":")
            assert 0 <= int(hh) < 24 and 0 <= int(mm) < 60
        except Exception:
            problems.append("SCHEDULE_TIME must look like HH:MM (24h).")
        if not self.fields_file.exists():
            problems.append(f"Fields file not found: {self.fields_file}")
        return problems


DEFAULT_SUBJECTS = {"BSE": "BSE Private Placement KIDs", "NSE": "NSE NCD New Issue KIDs"}
DEFAULT_FROM_NAMES = {"BSE": "BSE KID Bot", "NSE": "NSE KID Bot"}


def _exchange_email(code: str) -> Dict[str, object]:
    return {"to": _get_list(f"{code}_EMAIL_TO"), "cc": _get_list(f"{code}_EMAIL_CC"),
            "bcc": _get_list(f"{code}_EMAIL_BCC"), "subject": _get(f"{code}_EMAIL_SUBJECT_PREFIX"),
            "from": _get(f"{code}_EMAIL_FROM"), "from_name": _get(f"{code}_EMAIL_FROM_NAME")}


DEFAULT_KID_PATTERNS = [
    r"(?<![a-z])kid(?![a-z])",              # "1.Kid.pdf", "KID_2026.pdf", "Final KID.pdf"
    r"key[\s_\-]*information[\s_\-]*doc",   # "Key Information Document.pdf"
    r"key[\s_\-]*info[\s_\-]*doc",
]


def load_settings() -> Settings:
    s = Settings(
        timezone=_get("TIMEZONE", "Asia/Kolkata"),
        environment=_get("ENVIRONMENT", "production"),
        exchanges=[x.upper() for x in _get_list("EXCHANGES")] or ["BSE", "NSE"],
        nse_page_url=_get("NSE_PAGE_URL", Settings.nse_page_url),
        nse_api_url=_get("NSE_API_URL", Settings.nse_api_url),
        nse_base_url=_get("NSE_BASE_URL", Settings.nse_base_url),
        nse_index=_get("NSE_INDEX", "debt"),
        nse_document_types=_get_list("NSE_DOCUMENT_TYPES") or ["NCD-New Issue"],
        exchange_email={c: _exchange_email(c) for c in ("BSE", "NSE")},
        bse_page_url=_get("BSE_PAGE_URL", Settings.bse_page_url),
        bse_api_url=_get("BSE_API_URL", Settings.bse_api_url),
        bse_base_url=_get("BSE_BASE_URL", Settings.bse_base_url),
        bse_date_type=_get("BSE_DATE_TYPE", "1"),
        lookback_days=_get_int("LOOKBACK_DAYS", 1),
        fixed_date=parse_date(HARDCODED_DATE, "HARDCODED_DATE"),
        scraper_mode=(_get("SCRAPER_MODE", "auto") or "auto").lower(),
        browser_channels=_get("BROWSER_CHANNELS", "msedge,chrome,chromium"),
        browser_headless=_get_bool("BROWSER_HEADLESS", True),
        use_windows_cert_store=_get_bool("USE_WINDOWS_CERT_STORE", True),
        http_connect_timeout=_get_int("HTTP_CONNECT_TIMEOUT", 30),
        http_read_timeout=_get_int("HTTP_READ_TIMEOUT", 120),
        download_read_timeout=_get_int("DOWNLOAD_READ_TIMEOUT", 600),
        http_max_retries=_get_int("HTTP_MAX_RETRIES", 4),
        max_download_mb=_get_int("MAX_DOWNLOAD_MB", 1024),
        max_zip_depth=_get_int("MAX_ZIP_DEPTH", 8),
        max_extracted_mb=_get_int("MAX_EXTRACTED_MB", 4096),
        kid_filename_patterns=_get_list("KID_FILENAME_PATTERNS") or list(DEFAULT_KID_PATTERNS),
        kid_content_fallback=_get_bool("KID_CONTENT_FALLBACK", True),
        claude_enabled=_get_bool("CLAUDE_ENABLED", True),
        anthropic_api_key=get_secret("ANTHROPIC_API_KEY"),
        claude_model=_get("CLAUDE_MODEL", Settings.claude_model),
        claude_max_tokens=_get_int("CLAUDE_MAX_TOKENS", 4096),
        claude_timeout=_get_int("CLAUDE_TIMEOUT", 300),
        claude_max_retries=_get_int("CLAUDE_MAX_RETRIES", 5),
        claude_min_interval_sec=_get_float("CLAUDE_MIN_INTERVAL_SEC", 3.0),
        claude_max_pdf_pages=_get_int("CLAUDE_MAX_PDF_PAGES", 100),
        claude_max_pdf_mb=_get_float("CLAUDE_MAX_PDF_MB", 20.0),
        fields_file=_path("FIELDS_FILE", "config/kid_fields.json"),
        smtp_host=_get("SMTP_HOST", ""),
        smtp_port=_get_int("SMTP_PORT", 587),
        smtp_security=(_get("SMTP_SECURITY", "starttls") or "starttls").lower(),
        smtp_auth_method=(_get("SMTP_AUTH_METHOD", "") or "").upper(),
        smtp_username=_get("SMTP_USERNAME", ""),
        smtp_password=get_secret("SMTP_PASSWORD"),
        email_from=_get("EMAIL_FROM", ""),
        email_from_name=_get("EMAIL_FROM_NAME", ""),
        email_to=_get_list("EMAIL_TO"),
        email_cc=_get_list("EMAIL_CC"),
        email_bcc=_get_list("EMAIL_BCC"),
        email_subject_prefix=_get("EMAIL_SUBJECT_PREFIX", "BSE Private Placement KIDs"),
        email_send_when_empty=_get_bool("EMAIL_SEND_WHEN_EMPTY", True),
        email_attach_csv=_get_bool("EMAIL_ATTACH_CSV", True),
        email_attach_kids=_get_bool("EMAIL_ATTACH_KIDS", True),
        email_max_attachments_mb=_get_float("EMAIL_MAX_ATTACHMENTS_MB", 20.0),
        email_max_retries=_get_int("EMAIL_MAX_RETRIES", 3),
        schedule_time=_get("SCHEDULE_TIME", "08:00"),
        downloads_dir=_path("DOWNLOADS_DIR", "downloads"),
        extracted_dir=_path("EXTRACTED_DIR", "extracted"),
        processed_dir=_path("PROCESSED_DIR", "processed"),
        logs_dir=_path("LOGS_DIR", "logs"),
        retention_days=_get_int("RETENTION_DAYS", 30),
        output_dir=(_path("OUTPUT_DIR", "") if _get("OUTPUT_DIR") else None),
        output_retention_days=_get_int("OUTPUT_RETENTION_DAYS", 3),
        kid_archive_retention_days=_get_int("KID_ARCHIVE_RETENTION_DAYS", 0),
        log_retention_days=_get_int("LOG_RETENTION_DAYS", 90),
        log_level=(_get("LOG_LEVEL", "INFO") or "INFO").upper(),
        max_doc_attempts=_get_int("MAX_DOC_ATTEMPTS", 3),
        skip_already_emailed=_get_bool("SKIP_ALREADY_EMAILED", False),
    )
    return s
