"""
End-to-end pipeline:  BSE listing → download → recursive unzip → KID PDFs → Claude → email.

Failure isolation: every document and every KID is processed inside its own
try/except. One bad ZIP or one failed API call is logged, recorded in the state
DB and listed in the email's "needs attention" section; everything else carries on.

Exit codes (useful for Task Scheduler "Last Run Result"):
    0 = success, 1 = completed with some failures, 2 = fatal error (nothing processed)
"""
from __future__ import annotations

import json
import zipfile
import logging
import shutil
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .bse_scraper import BSEScraper, Document
from .claude_processor import ClaudeProcessor
from .config import DAY_FOLDER_FORMAT, Settings
from .email_service import EmailService, build_report
from .state import StateStore
from .utils import RunLock, who_is_locking
from .zip_processor import ZipProcessor

log = logging.getLogger("bse_kid.pipeline")

EXIT_OK, EXIT_PARTIAL, EXIT_FATAL = 0, 1, 2


@dataclass
class RunOptions:
    date_from: date
    date_to: date
    dry_run: bool = False       # download + extract only; no Claude, no email, no state changes to 'done'
    skip_email: bool = False
    force: bool = False         # ignore duplicate detection and reprocess
    limit: Optional[int] = None  # process at most N documents (testing)


@dataclass
class RunResult:
    results: List[Dict[str, Any]] = field(default_factory=list)
    failures: List[Dict[str, str]] = field(default_factory=list)
    stats: Dict[str, int] = field(default_factory=dict)
    exit_code: int = EXIT_OK
    report_dir: Optional[Path] = None
    # One entry per email: (zip to attach, the result rows describing what is in that zip)
    email_parts: List[Tuple[Path, List[Dict[str, Any]]]] = field(default_factory=list)

    @property
    def email_zips(self) -> List[Path]:
        return [z for z, _ in self.email_parts]


def default_dates(settings: Settings) -> tuple[date, date]:
    """Yesterday (in the configured timezone), optionally extended by LOOKBACK_DAYS."""
    # HARDCODED_DATE in src/config.py; blank = normal behaviour (yesterday).
    if settings.fixed_date:
        log.warning("HARDCODED_DATE is set in src/config.py -> using %s instead of yesterday",
                    settings.fixed_date.strftime("%d-%m-%Y"))
        return settings.fixed_date, settings.fixed_date
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    yesterday = today - timedelta(days=1)
    return yesterday - timedelta(days=settings.lookback_days - 1), yesterday


def period_label(d1: date, d2: date) -> str:
    return d1.strftime("%d-%m-%Y") if d1 == d2 else f"{d1:%d-%m-%Y} to {d2:%d-%m-%Y}"


class Pipeline:
    def __init__(self, settings: Settings, scraper: Optional[BSEScraper] = None,
                 claude: Optional[ClaudeProcessor] = None, emailer: Optional[EmailService] = None):
        self.s = settings
        self.scraper = scraper or BSEScraper(settings)
        self.zipper = ZipProcessor(settings)
        self._claude = claude
        self.emailer = emailer or EmailService(settings)
        self.fields = settings.load_fields()

    @property
    def claude(self) -> ClaudeProcessor:
        if self._claude is None:
            self._claude = ClaudeProcessor(self.s)
        return self._claude

    # ================================================================== run
    def run(self, opts: RunOptions) -> RunResult:
        self.s.ensure_dirs()
        with RunLock(self.s.lock_file):
            store = StateStore(self.s.state_db)
            try:
                return self._run(opts, store)
            finally:
                store.close()
                try:
                    self.scraper.close()
                except Exception:
                    pass

    def _run(self, opts: RunOptions, store: StateStore) -> RunResult:
        t0 = time.time()
        label = period_label(opts.date_from, opts.date_to)
        log.info("=" * 78)
        log.info("RUN START  period=%s  dry_run=%s  skip_email=%s  force=%s",
                 label, opts.dry_run, opts.skip_email, opts.force)
        run_id = store.start_run(str(opts.date_from), str(opts.date_to))
        rr = RunResult()
        st = dict(documents_listed=0, downloaded=0, skipped_duplicates=0, kids_found=0,
                  kids_extracted=0, kids_reused=0, failures=0)

        # 0. anything left unsent from a previous run?
        if not opts.dry_run and not opts.skip_email:
            pending = list(self.s.outbox_dir.glob("pending_*.eml"))
            if pending:
                log.info("Found %d unsent email(s) from earlier runs — retrying", len(pending))
                self.emailer.resend_pending()

        # 1. listing
        try:
            docs = self.scraper.fetch_documents(opts.date_from, opts.date_to)
        except Exception as exc:
            log.exception("FATAL: could not get the BSE listing")
            rr.failures.append({"item": "BSE listing", "error": str(exc)})
            rr.exit_code = EXIT_FATAL
            st["failures"] = 1
            rr.stats = self._pretty_stats(st)
            if not opts.dry_run and not opts.skip_email:
                self._send(rr, label, store)
            store.finish_run(run_id, "fatal", {**st, "error": str(exc)})
            return rr

        st["documents_listed"] = len(docs)

        # Carry-over: documents from earlier runs that failed and still have attempts left.
        if not opts.dry_run:
            listed = {d.url for d in docs}
            for row in store.retryable_documents(self.s.max_doc_attempts):
                if row["url"] in listed:
                    continue
                docs.append(Document(url=row["url"], company=row["company"] or "",
                                     isins=json.loads(row["isins"] or "[]"),
                                     allotment_dates=[x for x in (row["allotment_date"] or "").split(", ") if x]))
                log.info("Retrying earlier failed document: %s (attempt %d)", row["company"], row["attempts"] + 1)

        if opts.limit:
            docs = docs[: opts.limit]

        # Day folder = the (From) date searched on BSE. Layout (see Settings.day_dir):
        #   <day>/BSE/<Company>_<ISIN>/      package + full unzip
        #   <day>/BSE/KidFiles/              every KID found
        #   <day>/BSE/KidFilesZip_<day>.zip  the KIDs zipped; this is what is emailed
        day_dir = self.s.day_dir(opts.date_from)
        bse_dir = day_dir / "BSE"
        # Re-running a date starts from scratch: the old day folder is deleted first.
        # (Done only now, after the BSE listing succeeded, so a failed run keeps the old files.)
        if day_dir.exists():
            log.info("Folder for this date already exists - deleting it to re-run from scratch: %s", day_dir)
            shutil.rmtree(day_dir, onexc=_log_undeletable)
        log.info("Files for this run go to: %s", bse_dir)
        folder_names: Dict[str, str] = {}   # doc url -> folder name (unique within the run)
        if not self.s.claude_enabled and not opts.dry_run:
            log.info("CLAUDE_ENABLED=false: KIDs are not extracted, the PDFs are attached to the email instead")
        # 2. per document
        for idx, doc in enumerate(docs, 1):
            log.info("-" * 78)
            log.info("[%d/%d] %s  (%s)", idx, len(docs), doc.company, ", ".join(doc.isins))
            if not opts.force and self.s.skip_already_emailed:
                reason = store.should_skip_document(doc.url, self.s.max_doc_attempts)
                if reason:
                    log.info("Duplicate detection: skipping %s — %s", doc.file_name, reason)
                    st["skipped_duplicates"] += 1
                    continue
            store.upsert_document(doc.url, doc.company, doc.isins, ", ".join(doc.allotment_dates))
            name = doc.folder_name
            if name.lower() in {n.lower() for n in folder_names.values()}:
                name = f"{name}_{doc.doc_id}"   # same company + ISIN in two packages
            folder_names[doc.url] = name
            try:
                self._process_document(doc, bse_dir / name, bse_dir / "KidFiles", opts, store, rr, st)
            except Exception as exc:
                log.exception("Document failed: %s", doc.url)
                attempts = store.bump_document_attempt(doc.url, str(exc))
                rr.failures.append({"item": f"{doc.company} ({', '.join(doc.isins)})",
                                    "error": f"{exc} [attempt {attempts}/{self.s.max_doc_attempts}]"})

        # 3. include results that were extracted earlier but never emailed (e.g. crash mid-run)
        if not opts.dry_run:
            have = {r["sha256"] for r in rr.results}
            for row in store.unemailed_results():
                if row["sha256"] not in have and row["result_json"]:
                    data = json.loads(row["result_json"])
                    data["sha256"] = row["sha256"]
                    rr.results.append(data)
                    log.info("Including previously un-emailed result: %s", row["file_name"])

        st["failures"] = len(rr.failures)
        rr.stats = self._pretty_stats(st)
        if not opts.dry_run:
            rr.email_parts = self._zip_kids(rr, bse_dir, opts.date_from)
        rr.report_dir = self._write_report_files(rr, label, day_dir)

        # 4. email
        if opts.dry_run:
            log.info("Dry run: email not sent. Report preview saved in %s", rr.report_dir)
        elif opts.skip_email:
            log.info("--skip-email: email not sent. Report saved in %s", rr.report_dir)
        else:
            self._send(rr, label, store)

        rr.exit_code = EXIT_PARTIAL if rr.failures else EXIT_OK
        store.finish_run(run_id, "ok" if rr.exit_code == EXIT_OK else "partial", st)
        log.info("RUN END  %s  in %.0fs  exit=%d", json.dumps(st), time.time() - t0, rr.exit_code)

        # 5. housekeeping (never fails the run)
        try:
            cleanup(self.s)
        except Exception as exc:
            log.warning("Cleanup failed: %s", exc)
        return rr

    # ------------------------------------------------------------ helpers
    def _process_document(self, doc: Document, folder: Path, kid_dir: Path, opts: RunOptions,
                          store: StateStore, rr: RunResult, st: Dict[str, int]) -> None:
        """Everything for one document stays inside `folder` (<Company>_<ISIN>):
            download\\   the package from BSE + its full unzip
            output\\     the KID PDF(s) - exactly what is attached to the email"""
        path = self.scraper.download(doc, folder, name=folder.name)
        st["downloaded"] += 1
        store.update_document(doc.url, status="downloaded", zip_path=str(path))

        unzipped = folder / "unzipped"
        shutil.rmtree(unzipped, ignore_errors=True)   # start clean if this document is re-run
        # KIDs are copied to the shared KidFiles folder, prefixed with <Company>_<ISIN>
        kids = self.zipper.process(path, unzipped, kid_dir, label=folder.name)
        st["kids_found"] += len(kids)
        if not kids:
            if not opts.dry_run:
                store.update_document(doc.url, status="no_kid", last_error="No KID PDF in package")
            rr.failures.append({"item": f"{doc.company} ({', '.join(doc.isins)})",
                                "error": "No KID PDF found inside the downloaded package"})
            return

        all_ok = True
        for kid in kids:
            store.register_kid(kid.sha256, doc.url, kid.original_name, str(kid.path))
            source = {"company": doc.company, "isins": doc.isins, "allotment_dates": doc.allotment_dates,
                      "redemption_dates": doc.redemption_dates, "url": doc.url,
                      "kid_file": kid.original_name, "nesting": kid.nesting, "archive_path": str(kid.path)}
            if opts.dry_run:
                rr.results.append({"sha256": kid.sha256, "source": source,
                                   "fields": {f["key"]: "(dry run — not extracted)" for f in self.fields}})
                continue

            if not self.s.claude_enabled:
                existing = store.get_kid(kid.sha256)
                if (existing and existing["emailed_at"] and not opts.force
                        and self.s.skip_already_emailed):
                    log.info("KID %s already emailed on %s — not repeating", kid.original_name, existing["emailed_at"])
                    continue
                rr.results.append({"sha256": kid.sha256, "source": source, "fields": {}})
                continue

            existing = store.get_kid(kid.sha256)
            if existing and existing["status"] == "extracted" and not opts.force:
                st["kids_reused"] += 1   # reuse the earlier extraction: no new Claude call
                if existing["emailed_at"] and self.s.skip_already_emailed:
                    log.info("KID %s already extracted and emailed on %s — not repeating",
                             kid.original_name, existing["emailed_at"])
                else:
                    data = json.loads(existing["result_json"])
                    data["sha256"] = kid.sha256
                    rr.results.append(data)
                continue
            try:
                context = {"Company (BSE)": doc.company, "ISIN(s) (BSE)": ", ".join(doc.isins),
                           "Allotment date (BSE)": ", ".join(doc.allotment_dates),
                           "Redemption date(s) (BSE)": ", ".join(doc.redemption_dates)}
                out = self.claude.extract(kid.path, context)
                result = {"sha256": kid.sha256, "source": source, **out}
                store.save_kid_result(kid.sha256, result)
                rr.results.append(result)
                st["kids_extracted"] += 1
            except Exception as exc:
                all_ok = False
                log.exception("Claude extraction failed for %s", kid.nesting)
                store.fail_kid(kid.sha256, str(exc))
                rr.failures.append({"item": f"{doc.company} — {kid.original_name}", "error": f"Extraction failed: {exc}"})

        if opts.dry_run:
            return
        if all_ok:
            store.update_document(doc.url, status="done", last_error=None)
        else:
            store.bump_document_attempt(doc.url, "One or more KID extractions failed")

    @property
    def report_fields(self) -> List[Dict[str, Any]]:
        """Fields shown in the email/report; none when Claude extraction is switched off."""
        return self.fields if self.s.claude_enabled else []

    def _zip_kids(self, rr: RunResult, bse_dir: Path, day: date) -> List[Tuple[Path, List[Dict[str, Any]]]]:
        """Zip this run's KIDs into BSE/KidFilesZip_<day>.zip and plan the email(s).

        One email may carry EMAIL_MAX_ATTACHMENTS_MB (encoded size). When the zip is bigger,
        the KIDs are packed into KidFilesZip_<day>_partN.zip files, one per email, and each
        email lists exactly the KIDs in its part. A KID PDF that alone exceeds the limit is
        split by pages into BSE/KidFilesSplit/<name>_pagesX-Y.pdf so it can still be sent.

        Returns [(zip, rows)] - rows are copies of the results, describing that zip's files.
        Each result's source gets "zip" = where it went (None = could not be emailed)."""
        if not self.s.email_attach_kids:
            return []
        files = []
        for r in rr.results:
            r["source"]["zip"] = None
            p = Path(r["source"].get("archive_path", ""))
            if p.is_file():
                files.append((r, p))
        if not files:
            return []
        stem = f"KidFilesZip_{day.strftime(DAY_FOLDER_FORMAT)}"
        for old in bse_dir.glob(f"{stem}*.zip"):    # re-run of the same day: rebuild
            old.unlink()
        full = bse_dir / f"{stem}.zip"
        _write_zip(full, [p for _, p in files])
        log.info("Zipped %d KID(s) into %s (%.1f MB)", len(files), full, full.stat().st_size / 1e6)

        limit = self.s.email_max_attachments_mb * 1024 * 1024 * 3 / 4   # encoding adds 1/3
        if full.stat().st_size <= limit:
            for r, _ in files:
                r["source"]["zip"] = full.name
            return [(full, list(rr.results))]

        # Too big for one email: one item per file to send; oversized PDFs become page pieces.
        items: List[Tuple[Dict[str, Any], Path, str]] = []      # (result, file, note)
        for r, p in files:
            if p.stat().st_size <= limit:
                items.append((r, p, ""))
                continue
            pieces = _split_pdf(p, bse_dir / "KidFilesSplit", limit * 0.95)
            if not pieces:
                log.warning("KID %s (%.1f MB) is too large to email and could not be split; saved at %s",
                            p.name, p.stat().st_size / 1e6, p)
                continue
            log.info("KID %s (%.1f MB) split into %d PDF(s) by pages so it can be emailed",
                     p.name, p.stat().st_size / 1e6, len(pieces))
            for i, piece in enumerate(pieces, 1):
                items.append((r, piece, f"piece {i} of {len(pieces)}"))

        # Pack into as few emails as possible: biggest files first, each into the first email
        # with room (PDFs barely compress, so file size is a safe estimate). Within and across
        # emails the original order is kept, so part 1 starts with the first company.
        order = {id(item): n for n, item in enumerate(items)}
        bins: List[list] = []              # [used_bytes, [items]]
        for item in sorted(items, key=lambda it: it[1].stat().st_size, reverse=True):
            size = item[1].stat().st_size
            target = next((b for b in bins if b[0] + size <= limit), None)
            if target is None:
                target = [0, []]
                bins.append(target)
            target[0] += size
            target[1].append(item)
        groups = [sorted(b[1], key=lambda it: order[id(it)]) for b in bins]
        groups.sort(key=lambda g: order[id(g[0])])

        parts = []
        zips_of: Dict[int, List[str]] = {}
        for i, group in enumerate(groups, 1):
            part = bse_dir / f"{stem}_part{i}.zip"
            _write_zip(part, [p for _, p, _ in group])
            rows = []
            for r, p, note in group:
                zips_of.setdefault(id(r), []).append(part.name)
                src = {**r["source"], "zip": part.name}
                if note:
                    src["kid_file"] = f"{p.name} ({note})"
                rows.append({**r, "source": src})
            parts.append((part, rows))
        for r, _ in files:
            if id(r) in zips_of:
                r["source"]["zip"] = ", ".join(dict.fromkeys(zips_of[id(r)]))
        log.info("%s is over the %.0f MB email limit; split into %d part(s), one email each",
                 full.name, self.s.email_max_attachments_mb, len(parts))
        return parts

    def _send(self, rr: RunResult, label: str, store: StateStore) -> None:
        if not rr.results and not rr.failures and not self.s.email_send_when_empty:
            log.info("Nothing new and EMAIL_SEND_WHEN_EMPTY=false — no email sent")
            return
        csv_name = f"bse_kids_{label.replace(' ', '_')}.csv"
        parts, n = rr.email_parts, len(rr.email_parts)
        if n <= 1:
            report = build_report(rr.results, rr.failures, self.report_fields, label, rr.stats)
            msg = self.emailer.compose(report, csv_name=csv_name, attachments=[(z, z.name) for z, _ in parts])
            ok = self.emailer.send(msg)
        else:
            # One email per part; each lists exactly the KID files in its own zip.
            # Part 1 also carries the run's issues and any KID that could not be sent.
            unsent = [r for r in rr.results if not r["source"].get("zip")]
            ok, msg = True, None
            for i, (z, rows) in enumerate(parts, 1):
                note = (f"Email {i} of {n} for {label}: this email contains {len(rows)} KID file(s) "
                        f"in {z.name}. The other {n - 1} part(s) are sent as separate emails.")
                report = build_report(rows + (unsent if i == 1 else []), rr.failures if i == 1 else [],
                                      self.report_fields, label, rr.stats, part_note=note)
                report["subject"] = f"{report['subject']} (part {i} of {n})"
                part_msg = self.emailer.compose(report, csv_name=csv_name, attachments=[(z, z.name)])
                ok = self.emailer.send(part_msg) and ok
                msg = msg or part_msg
        if rr.report_dir:
            try:  # keep an exact copy of what was emailed (opens in Outlook)
                eml = rr.report_dir / f"email_{datetime.now():%H%M%S}_{'sent' if ok else 'FAILED'}.eml"
                eml.write_bytes(bytes(msg))
            except Exception as exc:
                log.warning("Could not save email copy: %s", exc)
        # Mark as emailed either way: a failed email is queued in the outbox and
        # re-sent later, so the same KIDs must not be emailed twice.
        store.mark_kids_emailed([r["sha256"] for r in rr.results if r.get("sha256")])
        if not ok:
            rr.failures.append({"item": "Email", "error": "Delivery failed — queued in processed/outbox"})

    def _write_report_files(self, rr: RunResult, label: str, out: Path) -> Path:
        out.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%H%M%S")
        report = build_report(rr.results, rr.failures, self.report_fields, label, rr.stats)
        (out / f"report_{stamp}.html").write_text(report["html"], encoding="utf-8")
        (out / f"results_{stamp}.csv").write_text(report["csv"], encoding="utf-8-sig")
        (out / f"results_{stamp}.json").write_text(
            json.dumps({"period": label, "stats": rr.stats, "results": rr.results, "failures": rr.failures},
                       indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("Report files written to %s", out)
        return out

    @staticmethod
    def _pretty_stats(st: Dict[str, int]) -> Dict[str, int]:
        stats = {
            "Documents listed": st["documents_listed"],
            "Downloaded": st["downloaded"],
            "KIDs found": st["kids_found"],
            "KIDs extracted": st["kids_extracted"] + st["kids_reused"],
            "Issues": st["failures"],
        }
        if st["skipped_duplicates"]:   # only possible with SKIP_ALREADY_EMAILED=true
            stats["Skipped (already done)"] = st["skipped_duplicates"]
        return stats


def _log_undeletable(func, path, exc) -> None:
    """rmtree error handler: a file open elsewhere (Outlook, Explorer, a PDF viewer) is
    left in place and simply overwritten by the new run."""
    log.warning("Could not delete %s (locked by %s) - it will be overwritten",
                path, who_is_locking(Path(path)) or "unknown program")


def _split_pdf(src: Path, out_dir: Path, max_bytes: float) -> List[Path]:
    """Split a PDF into consecutive page ranges, each file at most `max_bytes`:
    <name>_pages1-60.pdf, <name>_pages61-130.pdf ... Returns [] if that is impossible
    (not a readable PDF, or a single page is bigger than the limit)."""
    import io

    try:
        from pypdf import PdfReader, PdfWriter

        reader = PdfReader(str(src))
        if reader.is_encrypted:
            reader.decrypt("")
        n = len(reader.pages)
        per_page = src.stat().st_size / max(n, 1)
        out_dir.mkdir(parents=True, exist_ok=True)
        pieces: List[Path] = []
        start = 0
        while start < n:
            count = max(1, min(n - start, int(max_bytes * 0.85 / per_page)))
            while True:
                writer = PdfWriter()
                for i in range(start, start + count):
                    writer.add_page(reader.pages[i])
                buf = io.BytesIO()
                writer.write(buf)
                if buf.tell() <= max_bytes:
                    break
                if count == 1:
                    log.warning("Page %d of %s alone is over the email limit", start + 1, src.name)
                    return []
                count = max(1, count // 2)
            piece = out_dir / f"{src.stem}_pages{start + 1}-{start + count}.pdf"
            piece.write_bytes(buf.getvalue())
            pieces.append(piece)
            start += count
        return pieces
    except Exception as exc:
        log.warning("Could not split %s into smaller PDFs: %s", src.name, exc)
        return []


def _write_zip(path: Path, files: List[Path]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, arcname=f.name)


# ====================================================================== cleanup
def _older_than(p: Path, days: int) -> bool:
    return (time.time() - p.stat().st_mtime) > days * 86400


def cleanup(settings: Settings) -> None:
    """Delete dated working folders older than the retention period."""
    removed = 0
    targets = [(settings.downloads_dir, settings.retention_days),
               (settings.extracted_dir, settings.retention_days),
               (settings.reports_dir, max(settings.retention_days, 90))]
    if settings.kid_archive_retention_days > 0:
        targets.append((settings.kid_archive_dir, settings.kid_archive_retention_days))
    for base, days in targets:
        if not base.exists() or days <= 0:
            continue
        for child in base.iterdir():
            if child.name.startswith("."):
                continue
            try:
                if _older_than(child, days):
                    shutil.rmtree(child) if child.is_dir() else child.unlink()
                    removed += 1
            except Exception as exc:
                log.warning("Could not remove %s: %s", child, exc)
    root = settings.output_root
    if root.exists() and settings.output_retention_days > 0:
        for child in root.iterdir():
            try:
                datetime.strptime(child.name, DAY_FOLDER_FORMAT)
            except ValueError:
                continue  # never touch anything that isn't a YYYY_MM_DD day folder (e.g. log files)
            # Age = when the folder was last written, not the date in its name: a run for an
            # old date (e.g. 28-sep) must not delete the folder it has just filled.
            if child.is_dir() and _older_than(child, settings.output_retention_days):
                try:
                    shutil.rmtree(child)
                    removed += 1
                    log.info("Auto-deleted old output folder %s (older than %d days)", child, settings.output_retention_days)
                except Exception as exc:
                    log.warning("Could not delete %s: %s", child, exc)
    for f in settings.outbox_dir.glob("sent_*.eml"):
        if _older_than(f, 30):
            f.unlink(missing_ok=True)
    if removed:
        log.info("Retention cleanup removed %d old item(s)", removed)
