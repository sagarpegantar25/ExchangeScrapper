"""
Exchange registry.  Adding a new exchange = one new file here + one line in EXCHANGES.

Every scraper has the same interface (like an interface in C#):
    fetch_documents(date_from, date_to) -> list[Document]
    download(doc, target_dir, name=None) -> Path
    check_connectivity() -> dict
    close()
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict

from .bse import BSEScraper, Document  # noqa: F401  (Document re-exported)
from .nse import NSEScraper


@dataclass(frozen=True)
class ExchangeInfo:
    code: str              # "BSE" / "NSE" - also the sub-folder name in each day folder
    title: str             # heading of the email / report
    date_label: str        # what the searched date means on that site
    source: str            # shown in the report footer
    factory: Callable      # settings -> scraper


EXCHANGES: Dict[str, ExchangeInfo] = {
    "BSE": ExchangeInfo("BSE", "BSE Private Placement — Key Information Documents",
                        "Allotment date", "BSE memorandum of private placement bonds", BSEScraper),
    "NSE": ExchangeInfo("NSE", "NSE Debt Offer Documents (NCD-New Issue) — Key Information Documents",
                        "Filing date", "NSE corporate filings – offer documents (Debt)", NSEScraper),
}


def get_exchange(code: str) -> ExchangeInfo:
    try:
        return EXCHANGES[code.upper()]
    except KeyError:
        raise ValueError(f"Unknown exchange {code!r}; choose from {', '.join(EXCHANGES)}") from None
