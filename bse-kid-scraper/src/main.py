"""
Command-line entry point.  Run from the project root:

    python -m src.main                      # normal run: yesterday (the scheduled job)
    python -m src.main --date 2026-09-29    # a specific allotment date
    python -m src.main --from 2026-09-25 --to 2026-09-29
    python -m src.main --dry-run            # download + unzip + find KIDs only (no Claude, no email)
    python -m src.main --skip-email         # everything except sending the email
    python -m src.main --force              # reprocess even if already done
    python -m src.main --limit 1            # only the first document (quick test)
    python -m src.main --check              # validate config + test BSE, Claude and SMTP
    python -m src.main --test-email         # send a test email
    python -m src.main --resend-pending     # re-send emails that failed earlier
    python -m src.main --set-secret ANTHROPIC_API_KEY   # store a secret in Windows Credential Manager
    python -m src.main --status             # show recent runs and failed items
    python -m src.main --reset-url <URL>    # forget one document so it is reprocessed
    python -m src.main --cleanup            # apply retention now
    python -m src.main --daemon             # keep running, fire daily at SCHEDULE_TIME
"""
from __future__ import annotations

# Allow running this file directly (VS Code "Run Python File" / python src\main.py),
# not only as "python -m src.main". Sets up the package context so imports work.
if __package__ in (None, ""):
    import pathlib as _pl
    import sys as _sys

    _sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
    __package__ = "src"
    import src  # noqa: F401

import argparse
import getpass
import json
import sys
from datetime import date, datetime

from .config import DATE_INPUT_FORMATS, PROJECT_ROOT, ConfigError, load_settings, set_secret
from .logger import setup_logging


def _parse_date(s: str) -> date:
    for fmt in DATE_INPUT_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise argparse.ArgumentTypeError(f"Invalid date {s!r} (use YYYY-MM-DD or DD-MM-YYYY)")


def enable_windows_cert_store(settings, log) -> None:
    """Make Python trust the same certificates as Windows/Edge.

    Needed on corporate networks that inspect HTTPS (Zscaler, Fortinet, etc.); otherwise
    every request fails with CERTIFICATE_VERIFY_FAILED / self-signed certificate in chain.
    Applies to BSE, the Claude API and SMTP alike."""
    if not settings.use_windows_cert_store:
        return
    try:
        import truststore

        truststore.inject_into_ssl()
        log.debug("Using the operating system certificate store (truststore)")
    except ImportError:
        log.warning("truststore not installed — run: pip install -r requirements.txt")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bse-kid-scraper", description="BSE → KID PDF → Claude → email pipeline")
    p.add_argument("--date", type=_parse_date, help="single allotment date")
    p.add_argument("--from", dest="date_from", type=_parse_date, help="range start")
    p.add_argument("--to", dest="date_to", type=_parse_date, help="range end")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--skip-email", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--limit", type=int)
    p.add_argument("--check", action="store_true")
    p.add_argument("--test-email", action="store_true")
    p.add_argument("--diagnose-smtp", action="store_true", help="troubleshoot SMTP login (never prints the password)")
    p.add_argument("--resend-pending", action="store_true")
    p.add_argument("--set-secret", metavar="NAME", choices=["ANTHROPIC_API_KEY", "SMTP_PASSWORD"])
    p.add_argument("--status", action="store_true")
    p.add_argument("--reset-url", metavar="URL")
    p.add_argument("--cleanup", action="store_true")
    p.add_argument("--daemon", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.set_secret:
        print("Tip: paste with a RIGHT-CLICK (Ctrl+V may not work in a hidden prompt). Nothing is shown while typing.")
        value = getpass.getpass(f"Enter value for {args.set_secret} (input hidden): ").strip()
        if any(ord(c) < 32 for c in value):
            print("That input contains control characters (probably Ctrl+V was captured instead of the text).")
            print("Nothing was saved. Run the command again and paste with a right-click, or type it.")
            return 2
        if not value:
            print("Nothing entered — aborted.")
            return 2
        set_secret(args.set_secret, value)
        print(f"Saved {len(value)} characters.")
        print(f"{args.set_secret} saved to Windows Credential Manager (service 'bse-kid-scraper').")
        print(f"Remove {args.set_secret} from .env if it is there — the stored value is used when .env is blank.")
        return 0

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        return 2
    settings.ensure_dirs()
    log = setup_logging(settings.logs_dir, settings.log_level, settings.log_retention_days)
    enable_windows_cert_store(settings, log)

    # late imports so --set-secret works even if other deps are missing
    from .email_service import EmailService
    from .pipeline import EXIT_FATAL, Pipeline, RunOptions, cleanup, default_dates
    from .scheduler import run_daemon, task_scheduler_hint
    from .state import StateStore

    if args.check:
        return run_check(settings)

    if args.diagnose_smtp:
        from .email_service import diagnose_smtp

        return diagnose_smtp(settings)

    if args.test_email:
        problems = settings.validate(need_claude=False, need_email=True)
        if problems:
            for pr in problems:
                log.error("Config: %s", pr)
            return 2
        return 0 if EmailService(settings).send_test() else 1

    if args.resend_pending:
        n = EmailService(settings).resend_pending()
        log.info("Re-sent %d pending email(s)", n)
        return 0

    if args.status:
        store = StateStore(settings.state_db)
        print("\nRecent runs:")
        for r in store.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 10"):
            print(f"  #{r['id']} {r['started_at']} → {r['finished_at'] or '…'}  [{r['status']}]  "
                  f"{r['date_from']}..{r['date_to']}  {r['summary'] or ''}")
        print("\nDocuments needing attention:")
        for r in store.conn.execute("SELECT * FROM documents WHERE status NOT IN ('done') ORDER BY updated_at DESC LIMIT 20"):
            print(f"  [{r['status']}] attempts={r['attempts']} {r['company']} {r['url']}\n      {r['last_error'] or ''}")
        print("\nFailed KIDs:")
        for r in store.conn.execute("SELECT * FROM kids WHERE status='failed' ORDER BY updated_at DESC LIMIT 20"):
            print(f"  {r['file_name']} attempts={r['attempts']}  {r['last_error']}")
        counts = store.conn.execute("SELECT status, COUNT(*) c FROM documents GROUP BY status").fetchall()
        print("\nDocument totals:", {r["status"]: r["c"] for r in counts})
        return 0

    if args.reset_url:
        store = StateStore(settings.state_db)
        store.reset_document(args.reset_url)
        log.info("Document forgotten; it will be processed again on the next run: %s", args.reset_url)
        return 0

    if args.cleanup:
        cleanup(settings)
        return 0

    # ---------------------------------------------------------------- run
    need_claude = not args.dry_run
    need_email = not (args.dry_run or args.skip_email)
    problems = settings.validate(need_claude=need_claude, need_email=need_email)
    if problems:
        for pr in problems:
            log.error("Config: %s", pr)
        log.error("Fix the settings above in .env (see README) and try again.")
        return EXIT_FATAL

    def job() -> int:
        if args.date:
            d1 = d2 = args.date
        elif args.date_from or args.date_to:
            d1 = args.date_from or args.date_to
            d2 = args.date_to or args.date_from
        else:
            d1, d2 = default_dates(settings)
        if d1 > d2:
            d1, d2 = d2, d1
        opts = RunOptions(date_from=d1, date_to=d2, dry_run=args.dry_run,
                          skip_email=args.skip_email, force=args.force, limit=args.limit)
        try:
            result = Pipeline(settings).run(opts)
        except RuntimeError as exc:  # lock held
            log.error("%s", exc)
            return EXIT_FATAL
        except Exception:
            log.exception("Unexpected fatal error")
            return EXIT_FATAL
        log.info("Summary: %s", json.dumps(result.stats))
        if result.report_dir:
            log.info("Report files: %s", result.report_dir)
        return result.exit_code

    if args.daemon:
        log.info(task_scheduler_hint(settings, str(PROJECT_ROOT)))
        run_daemon(settings, job)
        return 0
    return job()


def run_check(settings) -> int:
    """Validate config and test every external dependency without processing anything."""
    import logging

    log = logging.getLogger("bse_kid.check")
    ok = True
    log.info("Project root: %s", PROJECT_ROOT)
    for pr in settings.validate():
        log.error("Config problem: %s", pr)
        ok = False
    log.info("Recipients: To=%s Cc=%s Bcc=%s", settings.email_to, settings.email_cc, settings.email_bcc)
    log.info("Schedule time: %s (%s); lookback days: %d", settings.schedule_time, settings.timezone, settings.lookback_days)

    from .bse_scraper import BSEScraper

    res = BSEScraper(settings).check_connectivity()
    log.info("BSE connectivity: %s", res)
    if any(v.startswith("ERROR") for v in res.values()):
        ok = False

    if settings.anthropic_api_key:
        try:
            from .claude_processor import ClaudeProcessor

            reply = ClaudeProcessor(settings).ping()
            log.info("Claude API OK (model=%s): %r", settings.claude_model, reply)
        except Exception as exc:
            log.error("Claude API check failed: %s", exc)
            ok = False

    if settings.smtp_host:
        import smtplib
        import ssl

        try:
            if settings.smtp_security == "ssl":
                srv = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30,
                                       context=ssl.create_default_context())
            else:
                srv = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
                srv.ehlo()
                if settings.smtp_security == "starttls":
                    srv.starttls(context=ssl.create_default_context())
            if settings.smtp_username:
                srv.login(settings.smtp_username, settings.smtp_password or "")
            srv.quit()
            log.info("SMTP login OK (%s:%s)", settings.smtp_host, settings.smtp_port)
        except Exception as exc:
            log.error("SMTP check failed: %s", exc)
            ok = False

    try:
        import playwright  # noqa: F401

        log.info("Playwright installed (browser fallback available)")
    except ImportError:
        log.warning("Playwright not installed — browser fallback unavailable (API mode still works)")

    log.info("CHECK %s", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
