"""
Offline tests — no BSE, Claude or real email needed.

    pip install -r requirements-dev.txt
    python -m pytest -v
"""
from __future__ import annotations

import email
import io
import os

import shutil
import sys
import zipfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.bse_scraper import BSEScraper, Document  # noqa: E402
from src.claude_processor import ClaudeProcessor, normalise_date  # noqa: E402
from src.config import Settings, DEFAULT_KID_PATTERNS  # noqa: E402
from src.email_service import EmailService, build_report  # noqa: E402
from src.pipeline import Pipeline, RunOptions  # noqa: E402
from src.state import StateStore  # noqa: E402
from src.zip_processor import ZipProcessor  # noqa: E402


# ----------------------------------------------------------------- fixtures
def make_pdf(text: str) -> bytes:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 750, text)
    c.showPage()
    c.save()
    return buf.getvalue()


def make_bse_zip(path: Path, kid_name: str = "1.Kid.pdf", kid_text: str = "Key Information Document") -> Path:
    """Main.zip -> Inner.zip -> 1.Kid.pdf  (same shape as the real BSE packages)."""
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr(kid_name, make_pdf(kid_text + " - Series A NCD"))
        z.writestr("2.GID.pdf", make_pdf("General Information Document"))
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("CMES GID NEW_20260929161610.zip", inner.getvalue())
        z.writestr("Credit rating_20260925145122.pdf", make_pdf("Rating letter"))
        z.writestr("DT Consent_20260925145122.pdf", make_pdf("Trustee consent"))
    return path


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(
        downloads_dir=tmp_path / "downloads", extracted_dir=tmp_path / "extracted",
        processed_dir=tmp_path / "processed", logs_dir=tmp_path / "logs",
        kid_filename_patterns=list(DEFAULT_KID_PATTERNS),
        fields_file=ROOT / "config" / "kid_fields.json",
        anthropic_api_key="test", claude_min_interval_sec=0,
        smtp_host="127.0.0.1", smtp_port=0, smtp_security="none",
        email_from="bot@example.com", email_to=["a@example.com", "b@example.com"],
        email_cc=["c@example.com"], email_max_retries=1,
    )
    s.ensure_dirs()
    return s


class FakeScraper(BSEScraper):
    def __init__(self, settings, docs_and_files):
        super().__init__(settings)
        self.items = docs_and_files

    def fetch_documents(self, d1, d2):
        return [d for d, _ in self.items]

    def download(self, doc, target_dir, name=None):
        src = dict((d.url, f) for d, f in self.items)[doc.url]
        target_dir.mkdir(parents=True, exist_ok=True)
        dest = target_dir / (f"{name}.zip" if name else f"{doc.doc_id}_{doc.file_name}")
        shutil.copy(src, dest)
        return dest


class FakeClaudeClient:
    def __init__(self, fail_times: int = 0):
        self.calls = 0
        self.fail_times = fail_times
        self.messages = self

    def create(self, **kw):
        self.calls += 1
        assert kw["tool_choice"]["name"] == "record_kid_data"
        assert kw["messages"][0]["content"][0]["type"] == "document"
        if self.calls <= self.fail_times:
            import anthropic
            try:
                import httpx2 as httpx  # anthropic >= 1.x ships on httpx2
            except ImportError:
                import httpx

            req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            raise anthropic.RateLimitError("rate limited", response=httpx.Response(429, request=req,
                                           headers={"retry-after": "0"}), body=None)
        tool = SimpleNamespace(type="tool_use", name="record_kid_data", input={
            "issuer_name": "Clean Max Enviro Energy Solutions Limited",
            "isins": ["INE647U07130", "INE647U07148"],
            "coupon_rate": "9.10% p.a.", "tenor": "10 years",
            "deemed_allotment_date": "28th September 2026", "redemption_date": "2036-09-28",
            "credit_rating": "CRISIL AA-/Stable", "total_issue_size": "INR 500 crore",
            "is_kid_document": True, "extraction_notes": "",
        })
        return SimpleNamespace(content=[tool], model="fake", stop_reason="tool_use",
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=200))


@pytest.fixture
def smtp_server():
    from aiosmtpd.controller import Controller

    class Handler:
        def __init__(self):
            self.messages = []

        async def handle_DATA(self, server, session, envelope):
            self.messages.append((envelope.rcpt_tos, envelope.content))
            return "250 OK"

    h = Handler()
    ctl = Controller(h, hostname="127.0.0.1", port=8025)
    ctl.start()
    yield h, 8025
    ctl.stop()


# ----------------------------------------------------------------- unit tests
def test_parse_api_payload_groups_shared_zip():
    payload = {"Table": [
        {"name": "Clean Max", "date1": "2026-09-28T00:00:00", "date2": "2036-09-28T00:00:00",
         "FILE1": "/corporates/download/12756/PPDIDisclosureDocuments_20260929174501.zip", "ISIN": "INE647U07130", "IsNotAvailable": 0},
        {"name": "Clean Max", "date1": "2026-09-28T00:00:00", "date2": "2028-09-28T00:00:00",
         "FILE1": "/corporates/download/12756/PPDIDisclosureDocuments_20260929174501.zip", "ISIN": "INE647U07106", "IsNotAvailable": 0},
        {"name": "No file", "FILE1": None, "ISIN": "X"},
    ]}
    issues = BSEScraper.parse_api_payload(payload, "https://www.bseindia.com")
    assert len(issues) == 2
    assert issues[0].file_url == "https://www.bseindia.com/corporates/download/12756/PPDIDisclosureDocuments_20260929174501.zip"
    docs = BSEScraper._group(issues)
    assert len(docs) == 1 and docs[0].isins == ["INE647U07130", "INE647U07106"]
    assert docs[0].doc_id == "12756"
    assert BSEScraper.parse_api_payload({}, "https://www.bseindia.com") == []


@pytest.mark.parametrize("name,expected", [
    ("1.Kid.pdf", True), ("KID_20260929.pdf", True), ("Final KID.PDF", True),
    ("Key Information Document.pdf", True), ("KeyInformationDocument.pdf", True),
    ("2.GID.pdf", False), ("Kidney report.pdf", False), ("DTA,GID,KID, FS.zip", False),
    ("Credit rating.pdf", False),
])
def test_kid_name_detection(settings, name, expected):
    assert ZipProcessor(settings).is_kid_name(name) is expected


def test_recursive_zip_finds_nested_kid(settings, tmp_path):
    z = make_bse_zip(tmp_path / "PPDIDisclosureDocuments_20260929174501.zip")
    kids = ZipProcessor(settings).process(z, tmp_path / "work", tmp_path / "archive", label="12756")
    assert len(kids) == 1
    assert kids[0].original_name == "1.Kid.pdf"
    assert "CMES GID NEW_20260929161610.zip > 1.Kid.pdf" in kids[0].nesting
    assert kids[0].path.exists()


def test_three_level_nesting(settings, tmp_path):
    lvl3 = io.BytesIO()
    with zipfile.ZipFile(lvl3, "w") as z:
        z.writestr("docs/KID.pdf", make_pdf("Key Information Document"))
    lvl2 = io.BytesIO()
    with zipfile.ZipFile(lvl2, "w") as z:
        z.writestr("deep.zip", lvl3.getvalue())
    p = tmp_path / "top.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("middle.zip", lvl2.getvalue())
        z.writestr("../evil.pdf", b"zip-slip attempt")  # must be neutralised
    kids = ZipProcessor(settings).process(p, tmp_path / "w", tmp_path / "a", label="1")
    assert len(kids) == 1 and kids[0].nesting.endswith("docs/KID.pdf")
    assert not (tmp_path / "evil.pdf").exists()


def test_content_fallback(settings, tmp_path):
    p = tmp_path / "pkg.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("Annexure_A.pdf", make_pdf("KEY INFORMATION DOCUMENT for private placement"))
        z.writestr("Other.pdf", make_pdf("Board resolution"))
    kids = ZipProcessor(settings).process(p, tmp_path / "w", tmp_path / "a", label="1")
    assert [k.original_name for k in kids] == ["Annexure_A.pdf"]
    assert kids[0].matched_by == "content"


def test_corrupt_zip_does_not_crash(settings, tmp_path):
    p = tmp_path / "bad.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("inner.zip", b"this is not a zip")
        z.writestr("3.KID.pdf", make_pdf("Key Information Document"))
    kids = ZipProcessor(settings).process(p, tmp_path / "w", tmp_path / "a", label="1")
    assert len(kids) == 1


def test_normalise(settings):
    cp = ClaudeProcessor(settings, client=FakeClaudeClient())
    out = cp.normalise({"issuer_name": "  ABC  Ltd ", "isins": "INE1, INE2; INE1",
                        "redemption_date": "28th September 2036", "coupon_rate": "N/A"})
    assert out["issuer_name"] == "ABC Ltd"
    assert out["isins"] == "INE1, INE2"
    assert out["redemption_date"] == "28-09-2036"
    assert out["coupon_rate"] == "Not found"
    assert out["tenor"] == "Not found"
    assert normalise_date("2026-09-28") == "28-09-2026"
    assert normalise_date("On T+1") == "On T+1"


def test_claude_retries_on_rate_limit(settings, tmp_path):
    pdf = tmp_path / "k.pdf"
    pdf.write_bytes(make_pdf("Key Information Document"))
    client = FakeClaudeClient(fail_times=2)
    res = ClaudeProcessor(settings, client=client).extract(pdf)
    assert client.calls == 3
    assert res["fields"]["coupon_rate"] == "9.10% p.a."
    assert res["fields"]["deemed_allotment_date"] == "28-09-2026"


def test_report_rendering(settings):
    fields = settings.load_fields()
    results = [{"sha256": "x", "source": {"company": "A & B <Ltd>", "isins": ["INE1"], "allotment_dates": ["29-09-2026"],
                                           "url": "https://x", "kid_file": "1.Kid.pdf"},
                "fields": {**{f["key"]: "v" for f in fields}, "issuer_name": "A & B <Ltd>"}}]
    rep = build_report(results, [{"item": "X", "error": "boom"}], fields, "29-09-2026", {"KIDs": 1})
    assert "A &amp; B &lt;Ltd&gt;" in rep["html"]          # escaped
    assert "BSE listing:" not in rep["html"]                # per-KID detail line removed

    no_fields = build_report(results, [], [], "29-09-2026", {"KIDs": 1})   # Claude switched off
    assert "class='card'" not in no_fields["html"] and "A &amp; B &lt;Ltd&gt;" in no_fields["html"]
    assert "1 KID, 1 issue" in rep["subject"]
    assert rep["csv"].count("\n") == 2


# ----------------------------------------------------------------- end to end
def test_end_to_end_with_dedup_and_email(settings, tmp_path, smtp_server):
    handler, port = smtp_server
    settings.smtp_port = port
    z1 = make_bse_zip(tmp_path / "a.zip")
    z2 = tmp_path / "broken.zip"
    z2.write_bytes(b"not a zip at all")
    docs = [
        (Document(url="https://www.bseindia.com/corporates/download/12756/PPDIDisclosureDocuments_1.zip",
                  company="Clean Max Enviro Energy Solutions Limited", isins=["INE647U07130", "INE647U07148"],
                  allotment_dates=["28-09-2026"]), z1),
        (Document(url="https://www.bseindia.com/corporates/download/999/PPDIDisclosureDocuments_2.zip",
                  company="Broken Co", isins=["INE000000000"]), z2),
    ]
    settings.skip_already_emailed = True
    client = FakeClaudeClient()
    pipe = Pipeline(settings, scraper=FakeScraper(settings, docs),
                    claude=ClaudeProcessor(settings, client=client), emailer=EmailService(settings))
    opts = RunOptions(date_from=date(2026, 9, 28), date_to=date(2026, 9, 28))

    r1 = pipe.run(opts)
    assert r1.exit_code == 1                      # partial: broken package listed as an issue
    assert len(r1.results) == 1 and client.calls == 1
    assert len(handler.messages) == 1
    rcpts, raw = handler.messages[0]
    assert set(rcpts) == {"a@example.com", "b@example.com", "c@example.com"}
    msg = email.message_from_bytes(raw)
    assert "BSE Private Placement KIDs | 28-09-2026" in msg["Subject"]
    parts = [p.get_content_type() for p in msg.walk()]
    assert "text/html" in parts and "text/csv" in parts
    html_part = next(p for p in msg.walk() if p.get_content_type() == "text/html").get_payload(decode=True).decode()
    assert "9.10% p.a." in html_part and "Broken Co" in html_part

    # Second run, same day: the good document must NOT be re-processed or re-emailed.
    r2 = pipe.run(opts)
    assert client.calls == 1
    assert r2.stats["Skipped (already done)"] == 1
    assert all(res["source"]["company"] != "Clean Max Enviro Energy Solutions Limited" for res in r2.results)

    store = StateStore(settings.state_db)
    row = store.get_document(docs[0][0].url)
    assert row["status"] == "done"
    assert store.get_document(docs[1][0].url)["attempts"] >= 2
    store.close()


def test_email_failure_goes_to_outbox(settings, tmp_path):
    settings.smtp_port = 1  # nothing listening
    svc = EmailService(settings)
    rep = build_report([], [], settings.load_fields(), "29-09-2026", {})
    assert svc.send(svc.compose(rep, "x.csv")) is False
    assert list(settings.outbox_dir.glob("pending_*.eml"))


def test_dry_run_does_not_mark_done(settings, tmp_path):
    z1 = make_bse_zip(tmp_path / "a.zip")
    doc = Document(url="https://www.bseindia.com/corporates/download/1/P.zip", company="X", isins=["I"])
    pipe = Pipeline(settings, scraper=FakeScraper(settings, [(doc, z1)]),
                    claude=ClaudeProcessor(settings, client=FakeClaudeClient()))
    r = pipe.run(RunOptions(date(2026, 9, 29), date(2026, 9, 29), dry_run=True))
    assert len(r.results) == 1 and r.exit_code == 0
    store = StateStore(settings.state_db)
    assert store.get_document(doc.url)["status"] == "downloaded"
    store.close()
    assert (r.report_dir / next(p.name for p in r.report_dir.glob("report_*.html"))).exists()


def test_bse_layout_kidfiles_zip_and_3day_cleanup(settings, tmp_path):
    import os
    import time as _time
    from src.pipeline import cleanup

    settings.output_dir = tmp_path / "logs"
    settings.output_retention_days = 3
    settings.ensure_dirs()
    z1 = make_bse_zip(tmp_path / "a.zip")
    z2 = make_bse_zip(tmp_path / "b.zip", kid_name="KID.pdf", kid_text="Key Information Document 2")
    with zipfile.ZipFile(z2, "a") as z:      # macOS junk must not be taken for a KID
        z.writestr("__MACOSX/._KID.pdf", b"junk")
    docs = [(Document(url="https://www.bseindia.com/corporates/download/5/P.zip", company="360 ONE Ltd",
                      isins=["INE0TQI07032"]), z1),
            (Document(url="https://www.bseindia.com/corporates/download/6/Q.zip", company="HELLA INFRA",
                      isins=["INE0P6G07194", "INE0P6G07202"]), z2)]
    # day folders last written 5 and 1 days ago; non-day folders are never touched
    old, recent = settings.output_dir / "2026_09_01", settings.output_dir / "2026_09_02"
    for d, age in ((old, 5), (recent, 1)):
        d.mkdir(parents=True)
        t = _time.time() - age * 86400
        os.utime(d, (t, t))
    (settings.output_dir / "my notes").mkdir()

    pipe = Pipeline(settings, scraper=FakeScraper(settings, docs),
                    claude=ClaudeProcessor(settings, client=FakeClaudeClient()))
    r = pipe.run(RunOptions(date(2026, 9, 17), date(2026, 9, 17), skip_email=True))

    day = settings.output_dir / "2026_09_17"        # named after the searched date, not today
    bse = day / "BSE"
    assert r.report_dir == day and list(day.glob("report_*.html"))
    assert sorted(p.name for p in bse.iterdir()) == [
        "360 ONE Ltd_INE0TQI07032", "HELLA INFRA_INE0P6G07194_+1", "KidFiles", "KidFilesZip_2026_09_17.zip"]
    pkg = bse / "360 ONE Ltd_INE0TQI07032"
    assert (pkg / "360 ONE Ltd_INE0TQI07032.zip").exists()
    unzipped = {p.name for p in (pkg / "unzipped").rglob("*") if p.is_file()}
    assert {"1.Kid.pdf", "2.GID.pdf", "Credit rating_20260925145122.pdf"} <= unzipped   # full unzip
    kid_files = sorted(p.name for p in (bse / "KidFiles").iterdir())
    assert kid_files == ["360 ONE Ltd_INE0TQI07032_1.Kid.pdf", "HELLA INFRA_INE0P6G07194_+1_KID.pdf"]
    with zipfile.ZipFile(bse / "KidFilesZip_2026_09_17.zip") as z:
        assert sorted(z.namelist()) == kid_files
    assert r.email_zips == [bse / "KidFilesZip_2026_09_17.zip"]

    cleanup(settings)                               # (also ran at the end of the run)
    names = sorted(p.name for p in settings.output_dir.iterdir())
    assert names == ["2026_09_02", "2026_09_17", "my notes"]


def test_without_claude_attaches_kid_pdf(settings, tmp_path, smtp_server):
    handler, port = smtp_server
    settings.smtp_port = port
    settings.claude_enabled = False
    settings.anthropic_api_key = None
    settings.email_attach_csv = False
    assert settings.validate() == []              # no API key needed when Claude is off
    z1 = make_bse_zip(tmp_path / "a.zip")
    doc = Document(url="https://www.bseindia.com/corporates/download/7/P.zip", company="Attach Co",
                   isins=["INE1"], allotment_dates=["29-09-2026"])
    pipe = Pipeline(settings, scraper=FakeScraper(settings, [(doc, z1)]))   # no Claude client at all
    opts = RunOptions(date(2026, 9, 29), date(2026, 9, 29))

    r1 = pipe.run(opts)
    assert r1.exit_code == 0 and len(r1.results) == 1
    assert len(handler.messages) == 1
    msg = email.message_from_bytes(handler.messages[0][1])
    types = [p.get_content_type() for p in msg.walk()]
    assert "application/pdf" not in types and "text/csv" not in types     # only the zip
    zips = [p for p in msg.walk() if p.get_content_type() == "application/zip"]
    assert [p.get_filename() for p in zips] == ["KidFilesZip_2026_09_29.zip"]
    with zipfile.ZipFile(io.BytesIO(zips[0].get_payload(decode=True))) as z:
        assert z.namelist() == ["Attach Co_INE1_1.Kid.pdf"]
    html_part = next(p for p in msg.walk() if p.get_content_type() == "text/html").get_payload(decode=True).decode()
    assert "In KidFilesZip_2026_09_29.zip" in html_part and "Not found" not in html_part

    # With SKIP_ALREADY_EMAILED=true: re-run skips the document, and the same KID under
    # another BSE listing is not emailed twice.
    settings.skip_already_emailed = True
    r2 = pipe.run(opts)
    assert r2.results == [] and r2.stats["Skipped (already done)"] == 1
    doc2 = Document(url="https://www.bseindia.com/corporates/download/8/P.zip", company="Attach Co", isins=["INE1"])
    pipe2 = Pipeline(settings, scraper=FakeScraper(settings, [(doc2, z1)]))
    assert pipe2.run(opts).results == []


def test_hardcoded_date_overrides_yesterday(settings):
    from src.pipeline import default_dates

    from src.config import parse_date

    assert parse_date("", "X") is None
    assert parse_date("17/09/2026", "X") == parse_date("17-sep-2026", "X") == date(2026, 9, 17)
    settings.fixed_date = parse_date("28-sep-2026", "X")
    assert default_dates(settings) == (date(2026, 9, 28), date(2026, 9, 28))
    settings.fixed_date = None
    d1, d2 = default_dates(settings)
    assert d1 == d2 and d2 < date.today()


def test_zip_too_big_for_one_email_is_split_into_parts(settings, tmp_path, smtp_server):
    handler, port = smtp_server
    settings.smtp_port = port
    settings.claude_enabled = False
    settings.email_attach_csv = False
    docs = []
    for i in range(3):
        z = tmp_path / f"p{i}.zip"
        with zipfile.ZipFile(z, "w") as zf:      # stored + random = incompressible, like real PDFs
            zf.writestr(f"KID{i}.pdf", b"%PDF" + os.urandom(300_000), compress_type=zipfile.ZIP_STORED)
        docs.append((Document(url=f"https://www.bseindia.com/corporates/download/{i}/P.zip",
                              company=f"Co{i}", isins=[f"INE{i}"]), z))
    # 0.6 MB encoded per email -> 0.45 MB of zip -> one 0.3 MB KID per part
    settings.email_max_attachments_mb = 0.6
    r = Pipeline(settings, scraper=FakeScraper(settings, docs)).run(RunOptions(date(2026, 9, 17), date(2026, 9, 17)))

    bse = r.report_dir / "BSE"
    assert (bse / "KidFilesZip_2026_09_17.zip").exists()                   # the full zip is kept
    assert [p.name for p in r.email_zips] == [f"KidFilesZip_2026_09_17_part{i}.zip" for i in (1, 2, 3)]
    subjects = [" ".join(email.message_from_bytes(raw)["Subject"].split()) for _, raw in handler.messages]
    assert len(subjects) == 3 and all(f"(part {i} of 3)" in sj for i, sj in enumerate(subjects, 1))


def test_rerun_resends_by_default(settings, tmp_path, smtp_server):
    handler, port = smtp_server
    settings.smtp_port = port
    settings.claude_enabled = False
    assert settings.skip_already_emailed is False                     # the default
    z1 = make_bse_zip(tmp_path / "a.zip")
    doc = Document(url="https://www.bseindia.com/corporates/download/9/P.zip", company="Again Co", isins=["INE9"])
    pipe = Pipeline(settings, scraper=FakeScraper(settings, [(doc, z1)]))
    opts = RunOptions(date(2026, 9, 17), date(2026, 9, 17))

    r1, r2 = pipe.run(opts), pipe.run(opts)
    assert len(r1.results) == len(r2.results) == 1                   # same KID both times
    assert len(handler.messages) == 2                                 # email sent again
    assert "Skipped (already done)" not in r2.stats
    html_part = next(p for p in email.message_from_bytes(handler.messages[1][1]).walk()
                     if p.get_content_type() == "text/html").get_payload(decode=True).decode()
    assert "Skipped" not in html_part and "Again Co" in html_part


def test_rerun_deletes_existing_day_folder_first(settings, tmp_path):
    settings.output_dir = tmp_path / "logs"
    settings.claude_enabled = False
    z1 = make_bse_zip(tmp_path / "a.zip")
    doc = Document(url="https://www.bseindia.com/corporates/download/9/P.zip", company="Fresh Co", isins=["INE9"])
    day = settings.output_dir / "2026_09_17"
    stale = day / "BSE" / "Old Co_INE1" / "old.zip"          # left over from an earlier run
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")
    (day / "report_000000.html").write_text("old report")

    Pipeline(settings, scraper=FakeScraper(settings, [(doc, z1)])).run(
        RunOptions(date(2026, 9, 17), date(2026, 9, 17), skip_email=True))

    assert not stale.parent.exists() and not (day / "report_000000.html").exists()
    assert sorted(p.name for p in (day / "BSE").iterdir()) == ["Fresh Co_INE9", "KidFiles", "KidFilesZip_2026_09_17.zip"]


def _html_of(raw):
    msg = email.message_from_bytes(raw)
    return next(p for p in msg.walk() if p.get_content_type() == "text/html").get_payload(decode=True).decode()


def test_each_part_email_lists_its_own_kids(settings, tmp_path, smtp_server):
    handler, port = smtp_server
    settings.smtp_port = port
    settings.claude_enabled = False
    settings.email_attach_csv = False
    docs = []
    for i in range(3):
        z = tmp_path / f"p{i}.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr(f"KID{i}.pdf", b"%PDF" + os.urandom(300_000), compress_type=zipfile.ZIP_STORED)
        docs.append((Document(url=f"https://www.bseindia.com/corporates/download/{i}/P.zip",
                              company=f"Company{i}", isins=[f"INE{i}"]), z))
    settings.email_max_attachments_mb = 0.6          # -> one 0.3 MB KID per email
    Pipeline(settings, scraper=FakeScraper(settings, docs)).run(RunOptions(date(2026, 9, 29), date(2026, 9, 29)))

    assert len(handler.messages) == 3
    for i, (_, raw) in enumerate(handler.messages):
        body = _html_of(raw)
        assert f"Email {i + 1} of 3" in body
        assert f"Company{i}" in body                                    # its own KID ...
        assert all(f"Company{j}" not in body for j in range(3) if j != i)   # ... and only that


def test_oversized_pdf_is_split_by_pages_and_sent(settings, tmp_path, smtp_server):
    from pypdf import PdfReader
    from reportlab.pdfgen import canvas

    handler, port = smtp_server
    settings.smtp_port = port
    settings.claude_enabled = False
    settings.email_attach_csv = False
    big = io.BytesIO()                                # 8 pages of ~100 KB incompressible text
    c = canvas.Canvas(big, pageCompression=0)
    for page in range(8):
        for line in range(200):
            c.drawString(10, 10 + line * 3, os.urandom(250).hex())
        c.showPage()
    c.save()
    z = tmp_path / "big.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("KID Big.pdf", big.getvalue(), compress_type=zipfile.ZIP_STORED)
    doc = Document(url="https://www.bseindia.com/corporates/download/1/P.zip", company="Grid Co", isins=["INE1"])
    settings.email_max_attachments_mb = 0.4          # ~0.3 MB per email; the PDF is ~0.9 MB (0.47 zipped)
    r = Pipeline(settings, scraper=FakeScraper(settings, [(doc, z)])).run(RunOptions(date(2026, 9, 29), date(2026, 9, 29)))

    bse = r.report_dir / "BSE"
    pieces = sorted((bse / "KidFilesSplit").glob("*.pdf"))
    assert len(pieces) >= 2 and all(p.stat().st_size <= 0.4 * 1024 * 1024 * 3 / 4 for p in pieces)
    assert sum(len(PdfReader(str(p)).pages) for p in pieces) == 8                 # no page lost
    assert len(handler.messages) == len(r.email_zips) >= 2                        # all pieces emailed
    assert "piece 1 of" in _html_of(handler.messages[0][1])
    assert (bse / "KidFiles" / "Grid Co_INE1_KID Big.pdf").exists()             # original kept
