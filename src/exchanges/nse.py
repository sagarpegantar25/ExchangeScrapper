"""
NSE "Corporate Filings - Offer Documents" scraper, Debt tab, TYPE = "NCD-New Issue".

How the page works (verified against the live site, Oct 2026):
  * https://www.nseindia.com/companies-listing/corporate-filings-offer-documents
    has tabs Equity / SME / Debt / ... The Debt tab is <a id="debt" data-variable="CFdebtOfferDocument">.
  * Clicking it makes the page call
        https://www.nseindia.com/api/corporates/offerdocs?index=debt&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY
    which returns a JSON list. One item per table row:
        {"issuer": "IndiaFirst Life Insurance Company Limited",
         "particulars": "Term Sheet - INE381Y08037 IFL 9.03% 2036",
         "date": "17-Sep-2026",                      <- the "Date" column (shown as a link)
         "attachFile": "https://nsearchives.nseindia.com/content/debt/DPP_NI_INE381Y08037_10092026.zip",
                                                     <- the link behind the date = the download
         "isin": "INE381Y08037",                     <- can be null
         "documentType": "NCD-New Issue",            <- the "Type" column
         "type": "PP"}
  * Types seen: "NCD-New Issue", "CP -  New issue", "CP -  Re-issue", "Trust Deed",
    "NCD-Re-issue", "Disclosure Document", ... Only NSE_DOCUMENT_TYPES are kept
    (default "NCD-New Issue"); spaces and case are ignored when comparing.
  * The same file can be listed twice (e.g. a shelf document) - it is downloaded once.
  * The downloads are ZIPs that usually hold ONE large PDF: sometimes "GID and KID" merged
    (found by name), sometimes only a GID (then no KID is reported for that issue).

NSE blocks plain scripts more aggressively than BSE: the API needs the cookies set by
visiting the site first, and may answer 401/403 or simply hang. So in "auto" mode the
direct API gets a short timeout, then the real browser (Edge) is used - same as BSE.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from datetime import date, datetime
from typing import Dict, List, Optional

import requests

from ..config import Settings
from ..utils import retry_call
from .bse import BSEBlockedError, BSEError, BSEScraper, Document

NSE_HEADERS = {
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-offer-documents",
    "Origin": "https://www.nseindia.com",
    "Accept": "application/json, text/plain, */*",
}


def _norm_type(t: str) -> str:
    """'NCD-New Issue', 'NCD - New issue', 'ncd-new issue' -> 'ncd-newissue'."""
    return re.sub(r"\s+", "", (t or "")).lower()


def _parse_nse_date(value) -> Optional[date]:
    s = str(value or "").strip()
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


class NSEScraper(BSEScraper):
    EXCHANGE = "NSE"
    READY_SELECTOR = "a#debt"              # the Debt tab

    @property
    def page_url(self) -> str:
        return self.s.nse_page_url

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.session.headers.update(NSE_HEADERS)

    # ================================================================ listing
    def fetch_documents(self, date_from: date, date_to: date) -> List[Document]:
        self.log.info("Fetching NSE Debt offer documents for %s → %s (types: %s, mode=%s)",
                      date_from, date_to, ", ".join(self.s.nse_document_types), self.s.scraper_mode)
        rows: Optional[list] = None
        errors = []
        if self.s.scraper_mode in ("api", "auto"):
            try:
                rows = self._fetch_via_api_nse(date_from, date_to)
            except Exception as exc:
                errors.append(f"api: {exc}")
                self.log.warning("NSE API listing failed: %s", exc)
                if self.s.scraper_mode == "api":
                    raise BSEError(f"NSE API listing failed: {exc}") from exc
                self.log.info("Switching to real-browser mode (listing and downloads)")
                self._use_browser_downloads = True
        if rows is None and self.s.scraper_mode in ("browser", "auto"):
            try:
                rows = self._fetch_via_browser_nse(date_from, date_to)
            except Exception as exc:
                errors.append(f"browser: {exc}")
                self.log.error("NSE browser listing failed: %s", exc)
        if rows is None:
            raise BSEError("Could not fetch NSE listing — " + "; ".join(errors))

        docs = self.parse_rows(rows, self.s.nse_document_types, date_from, date_to, self.log)
        self.log.info("Documents found: %d %s row(s) → %d unique download(s)",
                      sum(len(d.isins) or 1 for d in docs), " / ".join(self.s.nse_document_types), len(docs))
        for d in docs:
            self.log.info("  • %s | ISIN(s): %s | %s | %s", d.company, ", ".join(d.isins) or "-",
                          "; ".join(d.particulars), d.url)
        return docs

    def _api_url(self, date_from: date, date_to: date) -> str:
        # NSE uses DD-MM-YYYY, exactly like the page itself
        return (f"{self.s.nse_api_url}?index={self.s.nse_index}"
                f"&from_date={date_from:%d-%m-%Y}&to_date={date_to:%d-%m-%Y}")

    def _fetch_via_api_nse(self, date_from: date, date_to: date) -> list:
        timeout = (self.s.http_connect_timeout, min(self.s.http_read_timeout, 30))
        # NSE only answers its API after the site has set its cookies
        for warm in (self.s.nse_base_url, self.page_url):
            try:
                r = self.session.get(warm, timeout=timeout, headers={"Accept": "text/html,application/xhtml+xml"})
                self.log.info("NSE page access: HTTP %s (%s)", r.status_code, warm)
            except requests.RequestException as exc:
                self.log.warning("NSE page warm-up failed: %s", exc)
                break
        self.log.info("Date selection: From=%s To=%s (Debt tab) — querying", f"{date_from:%d-%m-%Y}",
                      f"{date_to:%d-%m-%Y}")
        url = self._api_url(date_from, date_to)

        def call() -> list:
            r = self.session.get(url, timeout=timeout)
            if r.status_code in (401, 403):
                raise BSEBlockedError(f"NSE refused the direct API request (HTTP {r.status_code})")
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict):        # defensive: some NSE APIs wrap lists in {"data": [...]}
                data = data.get("data") or []
            return data

        rows = retry_call(call, attempts=2, retry_on=(requests.RequestException, ValueError),
                          what="NSE listing API", logger=self.log)
        self.log.info("NSE API returned %d row(s)", len(rows))
        return rows

    def _fetch_via_browser_nse(self, date_from: date, date_to: date) -> list:
        """Human-like flow: open the page → click the Debt tab → query the dates from inside the page."""
        page = self._ensure_browser()
        try:
            if self._form_ready:
                with page.expect_response(lambda r: "offerdocs?index=debt" in r.url, timeout=30_000):
                    page.click(self.READY_SELECTOR)
                self.log.info("Clicked the Debt tab (CFdebtOfferDocument)")
        except Exception as exc:
            self.log.warning("Clicking the Debt tab did not trigger the expected request (%s); continuing", exc)
        self.log.info("Querying NSE Debt listing %s → %s from inside the browser page",
                      f"{date_from:%d-%m-%Y}", f"{date_to:%d-%m-%Y}")
        data = page.evaluate(
            """async (u) => { const r = await fetch(u, {credentials: 'include'});
                              if (!r.ok) throw new Error('HTTP ' + r.status); return await r.json(); }""",
            self._api_url(date_from, date_to),
        )
        if isinstance(data, dict):
            data = data.get("data") or []
        self.log.info("Browser listing returned %d row(s)", len(data))
        return data

    @staticmethod
    def parse_rows(rows: list, types: List[str], date_from: Optional[date] = None,
                   date_to: Optional[date] = None, logger=None) -> List[Document]:
        """Keep the wanted TYPEs within the date range; one Document per download link."""
        wanted = {_norm_type(t) for t in types}
        grouped: "OrderedDict[str, Document]" = OrderedDict()
        skipped: Dict[str, int] = {}
        for row in rows or []:
            dtype = (row.get("documentType") or "").strip()
            if _norm_type(dtype) not in wanted:
                skipped[dtype or "?"] = skipped.get(dtype or "?", 0) + 1
                continue
            url = (row.get("attachFile") or "").strip()
            if not url:
                if logger:
                    logger.warning("Row without download link skipped: %s", row.get("issuer"))
                continue
            d = _parse_nse_date(row.get("date"))
            if d and date_from and date_to and not (date_from <= d <= date_to):
                continue
            doc = grouped.get(url)
            if doc is None:
                doc = grouped[url] = Document(url=url, company=(row.get("issuer") or "").strip(), exchange="NSE")
            isin = (row.get("isin") or "").strip()
            if isin and isin not in doc.isins:
                doc.isins.append(isin)
            for lst, val in ((doc.allotment_dates, d.strftime("%d-%m-%Y") if d else ""),
                             (doc.doc_types, dtype), (doc.particulars, (row.get("particulars") or "").strip())):
                if val and val not in lst:
                    lst.append(val)
        if skipped and logger:
            logger.info("Ignored other types: %s", ", ".join(f"{k} ×{v}" for k, v in sorted(skipped.items())))
        return list(grouped.values())

    def check_connectivity(self) -> Dict[str, str]:
        out = {}
        try:
            r = self.session.get(self.page_url, timeout=(15, 30))
            out["page"] = f"HTTP {r.status_code}"
        except Exception as exc:
            out["page"] = f"ERROR {exc}"
        try:
            r = self.session.get(self._api_url(date(2026, 1, 1), date(2026, 1, 1)), timeout=(15, 30))
            out["api"] = f"HTTP {r.status_code}, {len(r.content)} bytes"
        except Exception as exc:
            out["api"] = f"ERROR {exc}"
        return out
