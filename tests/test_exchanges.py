"""
Tests for the ScraperAutomation additions: NSE source, separate emails per exchange,
exchange isolation, stricter KID detection and the "stuck files" folder fix.

    python -m pytest -v
"""
from __future__ import annotations

import email
import io
import sqlite3
import sys
import zipfile
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.exchanges import EXCHANGES, Document, get_exchange  # noqa: E402
from src.exchanges.nse import NSEScraper, _norm_type  # noqa: E402
from src.pipeline import Pipeline, RunOptions  # noqa: E402
from src.state import StateStore  # noqa: E402
from src.zip_processor import ZipProcessor  # noqa: E402
from test_pipeline import (FakeScraper, _files_only_layout, make_bse_zip, make_pdf,  # noqa: E402,F401
                           settings, smtp_server)

# Real rows returned by NSE for 17-09-2026 (api/corporates/offerdocs?index=debt), trimmed.
NSE_ROWS = [
    {"issuer": "Aditya Birla Money Limited", "particulars": "CP-NI-Disclosure Document-INE865C14RK0",
     "date": "17-Sep-2026", "attachFile": "https://nsearchives.nseindia.com/content/debt/CP_NI_ABML_INE865C14RK0_170926.zip",
     "isin": "INE865C14RK0", "documentType": "CP -  New issue", "type": "PP"},
    {"issuer": "IndiaFirst Life Insurance Company Limited", "particulars": "Term Sheet - INE381Y08037 IFL 9.03% 2036",
     "date": "17-Sep-2026", "attachFile": "https://nsearchives.nseindia.com/content/debt/DPP_NI_INE381Y08037_10092026.zip",
     "isin": "INE381Y08037", "documentType": "NCD-New Issue", "type": "PP"},
    {"issuer": "NIIF Infrastructure Finance Limited", "particulars": "Shelf Offer Document - 17-SEP-2026",
     "date": "17-Sep-2026", "attachFile": "https://nsearchives.nseindia.com/content/debt/NIFL_17-SEP-2026.zip",
     "isin": None, "documentType": "NCD-New Issue", "type": "PP"},
    {"issuer": "NIIF Infrastructure Finance Limited", "particulars": "Shelf Offer Document - 17-SEP-2026",
     "date": "17-Sep-2026", "attachFile": "https://nsearchives.nseindia.com/content/debt/NIFL_17-SEP-2026.zip",
     "isin": None, "documentType": "NCD-New Issue", "type": "PP"},
    {"issuer": "Tvs Credit Services Limited", "particulars": "Trust Deed", "date": "17-Sep-2026",
     "attachFile": "https://nsearchives.nseindia.com/content/debt/DPP_TCSL_TD_17092026.zip",
     "isin": "INE729N07115", "documentType": "Trust Deed", "type": "PP"},
    {"issuer": "Care Health Insurance Limited", "particulars": "Term Sheet - INE119J08032 CHI 10% 2036 Sr 2",
     "date": "17-Sep-2026", "attachFile": "https://nsearchives.nseindia.com/content/debt/DPP_NI_INE119J08032_27072026.zip",
     "isin": "INE119J08032", "documentType": "NCD-New Issue", "type": "PP"},
    {"issuer": "Old Co", "particulars": "Term Sheet", "date": "16-Sep-2026",
     "attachFile": "https://nsearchives.nseindia.com/content/debt/OLD.zip",
     "isin": "INE000000001", "documentType": "NCD-New Issue", "type": "PP"},
]


def nse_zip(path: Path, pdf_name: str, title: str, pages: int = 4) -> Path:
    """NSE packages: one ZIP holding one (large) PDF."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(pdf_name, make_pdf(title, pages=pages))
    return path


# ----------------------------------------------------------------- NSE listing
def test_nse_keeps_only_ncd_new_issue_and_dedups_links():
    docs = NSEScraper.parse_rows(NSE_ROWS, ["NCD-New Issue"], date(2026, 9, 17), date(2026, 9, 17))
    assert [d.company for d in docs] == ["IndiaFirst Life Insurance Company Limited",
                                         "NIIF Infrastructure Finance Limited", "Care Health Insurance Limited"]
    assert all(d.exchange == "NSE" and d.doc_types == ["NCD-New Issue"] for d in docs)
    nifl = docs[1]
    assert nifl.isins == [] and nifl.folder_name == "NIIF Infrastructure Finance Limited_NIFL_17-SEP-2026"
    assert docs[0].allotment_dates == ["17-09-2026"] and docs[0].particulars[0].startswith("Term Sheet")


def test_nse_type_matching_ignores_spaces_and_case():
    assert _norm_type("NCD-New Issue") == _norm_type("NCD - New issue") == _norm_type(" ncd-new  ISSUE ")
    assert _norm_type("NCD-Re-issue") != _norm_type("NCD-New Issue")
    docs = NSEScraper.parse_rows([{**NSE_ROWS[1], "documentType": "NCD - New issue"}], ["NCD-New Issue"])
    assert len(docs) == 1


def test_nse_api_url_uses_page_date_format(settings):
    url = NSEScraper(settings)._api_url(date(2026, 9, 17), date(2026, 9, 18))
    assert url == ("https://www.nseindia.com/api/corporates/offerdocs"
                   "?index=debt&from_date=17-09-2026&to_date=18-09-2026")


def test_registry():
    assert set(EXCHANGES) == {"BSE", "NSE"} and get_exchange("nse").date_label == "Filing date"
    with pytest.raises(ValueError):
        get_exchange("MCX")


# ----------------------------------------------------------------- KID detection
def test_merged_gid_kid_found_by_name_gid_only_is_not_a_kid(settings, tmp_path):
    zp = ZipProcessor(settings)
    merged = nse_zip(tmp_path / "a.zip", "5__CHIL_GID_KID_merged.pdf", "General Information Document")
    kids = zp.process(merged, tmp_path / "w1", tmp_path / "k1", label="CHI")
    assert [k.original_name for k in kids] == ["5__CHIL_GID_KID_merged.pdf"]

    gid_only = nse_zip(tmp_path / "b.zip", "GIDAchintyaSL.pdf",
                       "GENERAL INFORMATION DOCUMENT to be read with the Key Information Document")
    assert zp.process(gid_only, tmp_path / "w2", tmp_path / "k2", label="ASL") == []
    assert zp.last_pdf_names == ["GIDAchintyaSL.pdf"]


def test_consent_letter_mentioning_kid_is_not_a_kid(settings, tmp_path):
    """Real BSE false positive: 'DT consent' letters mention the Key Information Document."""
    p = tmp_path / "pkg.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("DT consent_20260929121521.pdf", make_pdf("Key Information Document consent", pages=4))
        z.writestr("Consent letter from DT.pdf", make_pdf("Key Information Document", pages=4))
        z.writestr("Short note.pdf", make_pdf("Key Information Document"))       # 1 page: not a KID
    assert ZipProcessor(settings).process(p, tmp_path / "w", tmp_path / "k", label="X") == []


# ----------------------------------------------------------------- pipeline per exchange
def _nse_settings(settings, tmp_path):
    settings.output_dir = tmp_path / "logs"
    settings.claude_enabled = False
    return settings.for_exchange("NSE")


def test_nse_pipeline_folder_email_and_no_kid_message(settings, tmp_path, smtp_server):
    handler, port = smtp_server
    settings.smtp_port = port
    xs = _nse_settings(settings, tmp_path)
    docs = NSEScraper.parse_rows(NSE_ROWS, ["NCD-New Issue"], date(2026, 9, 17), date(2026, 9, 17))
    files = [nse_zip(tmp_path / "1.zip", "GID_and_KID_IndiaFirst_Life.pdf", "GID and KID"),
             nse_zip(tmp_path / "2.zip", "GID_Sep_2026.pdf", "General Information Document"),
             nse_zip(tmp_path / "3.zip", "5__CHIL_GID_KID_merged.pdf", "GID KID")]

    class FakeNSE(FakeScraper):
        EXCHANGE = "NSE"

    r = Pipeline(xs, scraper=FakeNSE(xs, list(zip(docs, files)))).run(RunOptions(date(2026, 9, 17), date(2026, 9, 17)))

    nse = xs.output_root / "2026_09_17" / "NSE"
    assert r.report_dir == nse and not (xs.output_root / "2026_09_17" / "BSE").exists()
    assert sorted(p.name for p in (nse / "KidFiles").iterdir()) == [
        "Care Health Insurance Limited_INE119J08032_5__CHIL_GID_KID_merged.pdf",
        "IndiaFirst Life Insurance Company Limited_INE381Y08037_GID_and_KID_IndiaFirst_Life.pdf"]
    assert any("NIIF" in f["item"] and "GID_Sep_2026.pdf" in f["error"] for f in r.failures)

    assert len(handler.messages) == 1
    msg = email.message_from_bytes(handler.messages[0][1])
    assert msg["Subject"].startswith("NSE NCD New Issue KIDs | 17-09-2026")
    html = next(p for p in msg.walk() if p.get_content_type() == "text/html").get_payload(decode=True).decode()
    assert "NSE Debt Offer Documents" in html and "Filing date" in html and "Term Sheet" in html
    names = [p.get_filename() for p in msg.walk() if p.get_filename()]
    assert "KidFilesZip_2026_09_17.zip" in names and "nse_kids_17-09-2026.csv" in names


def test_bse_and_nse_send_separate_emails_and_one_failure_does_not_stop_the_other(
        settings, tmp_path, smtp_server, monkeypatch):
    """main.py: one email per exchange; NSE crashing must not stop BSE."""
    import src.main as main_mod
    from src import exchanges

    handler, port = smtp_server
    settings.smtp_port = port
    settings.output_dir = tmp_path / "logs"
    settings.claude_enabled = False
    settings.exchange_email = {"NSE": {"to": ["nse-desk@example.com"]}}
    bse_zip = make_bse_zip(tmp_path / "b.zip")
    bse_doc = Document(url="https://www.bseindia.com/corporates/download/1/P.zip", company="BSE Co", isins=["INE1"])

    class BoomNSE(FakeScraper):
        EXCHANGE = "NSE"

        def fetch_documents(self, d1, d2):
            raise RuntimeError("NSE is down")

    fake = {"BSE": exchanges.ExchangeInfo("BSE", "BSE title", "Allotment date", "BSE",
                                          lambda s: FakeScraper(s, [(bse_doc, bse_zip)])),
            "NSE": exchanges.ExchangeInfo("NSE", "NSE title", "Filing date", "NSE", lambda s: BoomNSE(s, []))}
    monkeypatch.setattr(exchanges, "EXCHANGES", fake)
    monkeypatch.setattr(main_mod, "load_settings", lambda: settings)
    monkeypatch.setattr(main_mod, "setup_logging", lambda *a, **k: __import__("logging").getLogger("exch"))

    rc = main_mod.main(["--exchange", "ALL", "--date", "17-09-2026"])

    assert rc == 2                      # worst result (NSE listing failed)
    subjects = {email.message_from_bytes(raw)["Subject"].split(" | ")[0]: rcpts for rcpts, raw in handler.messages}
    assert set(subjects) == {"BSE Private Placement KIDs", "NSE NCD New Issue KIDs"}   # two separate emails
    assert subjects["NSE NCD New Issue KIDs"] == ["nse-desk@example.com", "c@example.com"]  # own To + shared Cc
    assert "a@example.com" in subjects["BSE Private Placement KIDs"]                    # BSE uses EMAIL_TO
    assert (settings.output_root / "2026_09_17" / "BSE" / "KidFiles").exists()


def test_stuck_old_folder_gets_a_fresh_name(settings, tmp_path, monkeypatch):
    """Windows: files held by antivirus can't be deleted; the run must not write over them."""
    import src.pipeline as pl

    settings.output_dir = tmp_path / "logs"
    settings.claude_enabled = False
    day = settings.output_root / "2026_09_17"
    (day / "BSE" / "Old Co_INE1").mkdir(parents=True)
    monkeypatch.setattr(pl, "rmtree", lambda path, handler: None)        # deletion "fails"
    doc = Document(url="https://www.bseindia.com/corporates/download/9/P.zip", company="Fresh Co", isins=["INE9"])
    r = Pipeline(settings, scraper=FakeScraper(settings, [(doc, make_bse_zip(tmp_path / "a.zip"))])).run(
        RunOptions(date(2026, 9, 17), date(2026, 9, 17), skip_email=True))
    assert r.report_dir.name.startswith("BSE_") and (r.report_dir / "KidFiles").exists()
    assert (day / "BSE" / "Old Co_INE1").exists()                         # untouched


def test_for_exchange_settings(settings):
    settings.exchange_email = {"NSE": {"to": ["n@x.com"], "subject": "Custom NSE"}}
    b, n = settings.for_exchange("BSE"), settings.for_exchange("nse")
    assert b.exchange == "BSE" and b.email_to == ["a@example.com", "b@example.com"]
    assert b.email_subject_prefix == "BSE Private Placement KIDs"
    assert n.exchange == "NSE" and n.email_to == ["n@x.com"] and n.email_subject_prefix == "Custom NSE"
    assert n.email_cc == ["c@example.com"]                       # shared value when not overridden


def test_old_bse_database_is_migrated(tmp_path):
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)          # schema of bse-kid-scraper (no exchange column)
    con.execute("""CREATE TABLE documents (url TEXT PRIMARY KEY, company TEXT, isins TEXT, allotment_date TEXT,
                   status TEXT NOT NULL DEFAULT 'new', zip_path TEXT, zip_sha256 TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                   last_error TEXT, first_seen TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    con.execute("INSERT INTO documents(url, company, isins, status, attempts, first_seen, updated_at) "
                "VALUES('u1','Old','[]','failed',1,datetime('now','localtime'),datetime('now','localtime'))")
    con.commit()
    con.close()
    st = StateStore(db)
    assert st.get_document("u1")["exchange"] == "BSE"
    assert [r["url"] for r in st.retryable_documents(3, exchange="BSE")] == ["u1"]
    assert st.retryable_documents(3, exchange="NSE") == []
    st.close()


def test_bse_and_nse_email_settings_are_fully_separate(settings):
    settings.email_to, settings.email_cc, settings.email_from = [], [], ""
    settings.exchange_email = {
        "BSE": {"to": ["bse-team@x.com"], "cc": ["bse-cc@x.com"], "from": "bse-bot@x.com", "from_name": "BSE Bot"},
        "NSE": {"to": ["nse-team@x.com", "nse-2@x.com"], "from": "nse-bot@x.com"},
    }
    b, n = settings.for_exchange("BSE"), settings.for_exchange("NSE")
    assert (b.email_to, b.email_cc, b.email_from, b.email_from_name) == (
        ["bse-team@x.com"], ["bse-cc@x.com"], "bse-bot@x.com", "BSE Bot")
    assert (n.email_to, n.email_cc, n.email_from, n.email_from_name) == (
        ["nse-team@x.com", "nse-2@x.com"], [], "nse-bot@x.com", "NSE KID Bot")
    assert settings.validate(need_claude=False) == []          # no shared EMAIL_TO needed
    settings.exchange_email["NSE"]["to"] = []
    assert any("NSE_EMAIL_TO" in p for p in settings.validate(need_claude=False))
