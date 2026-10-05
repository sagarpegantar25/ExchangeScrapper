"""
Build and send the daily report email.

* HTML body (summary table + one detail card per KID + failures section)
  with a plain-text alternative for clients that block HTML.
* Optional CSV attachment of all extracted fields (opens in Excel).
* Multiple To / Cc / Bcc recipients from .env.
* SMTP with STARTTLS (587), SSL (465) or plain (25); retries with backoff.
* If sending still fails, the full message is saved to processed/outbox/*.eml
  and re-sent automatically on the next run (or with --resend-pending).
"""
from __future__ import annotations

import csv
import html
import io
import os
import logging
import smtplib
import ssl
import time
from datetime import datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formataddr, make_msgid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import Settings

log = logging.getLogger("bse_kid.email")


class EmailError(Exception):
    pass


# ---------------------------------------------------------------- rendering
CSS = """
body{font-family:Segoe UI,Arial,sans-serif;color:#1f2937;margin:0;padding:0;background:#f5f6f8}
.wrap{max-width:1100px;margin:0 auto;padding:20px}
h1{font-size:20px;margin:0 0 4px;color:#0b2e59}
.sub{color:#6b7280;font-size:13px;margin-bottom:18px}
.stats td{padding:6px 14px;background:#fff;border:1px solid #e5e7eb;font-size:13px}
.stats b{font-size:18px;color:#0b2e59}
table.grid{border-collapse:collapse;width:100%;background:#fff;font-size:12.5px;margin-bottom:22px}
table.grid th{background:#0b2e59;color:#fff;text-align:left;padding:7px 8px;font-weight:600}
table.grid td{border:1px solid #e5e7eb;padding:6px 8px;vertical-align:top}
table.grid tr:nth-child(even) td{background:#f9fafb}
.card{background:#fff;border:1px solid #e5e7eb;border-left:4px solid #0b2e59;margin:0 0 18px;padding:12px 14px}
.card h2{font-size:15px;margin:0 0 2px;color:#0b2e59}
table.kv{border-collapse:collapse;width:100%;font-size:12.5px}
table.kv td{border-bottom:1px solid #f0f1f3;padding:5px 6px;vertical-align:top}
table.kv td.k{width:230px;color:#4b5563;font-weight:600}
.nf{color:#9ca3af}
.note{font-size:12px;color:#92400e;background:#fffbeb;border:1px solid #fde68a;padding:6px 8px;margin-top:8px}
.fail{background:#fef2f2;border:1px solid #fecaca;border-left:4px solid #b91c1c;padding:10px 14px;font-size:12.5px}
.foot{font-size:11px;color:#9ca3af;margin-top:24px}
"""


def _v(value: Any) -> str:
    s = "" if value is None else str(value)
    if s == "Not found" or not s:
        return '<span class="nf">Not found</span>'
    return html.escape(s)


def _attached_text(src: Dict[str, Any]) -> str:
    if "zip" not in src:
        return ""
    if src["zip"]:
        return f"In {src['zip']}"
    return f"Not emailed (too large) - saved at {src.get('archive_path', '')}"


def build_report(results: List[Dict[str, Any]], failures: List[Dict[str, str]], fields: List[Dict[str, Any]],
                 period_label: str, stats: Dict[str, int], part_note: str = "") -> Dict[str, Any]:
    """Return {'subject', 'html', 'text', 'csv'} for the given results."""
    summary_fields = [f for f in fields if f.get("summary")]
    n = len(results)
    # Without extracted fields (Claude switched off) the summary lists the BSE details instead.
    src_cols = [("Company", lambda s: s.get("company", "")), ("ISIN(s)", lambda s: ", ".join(s.get("isins", []))),
                ("Allotment", lambda s: ", ".join(s.get("allotment_dates", []))),
                ("KID file", lambda s: s.get("kid_file", "")), ("Emailed in", _attached_text)]

    # ---------- HTML
    parts = [f"<html><head><meta charset='utf-8'><style>{CSS}</style></head><body><div class='wrap'>"]
    parts.append("<h1>BSE Private Placement — Key Information Documents</h1>")
    parts.append(f"<div class='sub'>Allotment date: <b>{html.escape(period_label)}</b> &nbsp;·&nbsp; "
                 f"Generated {datetime.now().strftime('%d-%b-%Y %H:%M')}</div>")
    if part_note:
        parts.append(f"<div class='note'><b>{html.escape(part_note)}</b></div><br>")
    parts.append("<table class='stats'><tr>" + "".join(
        f"<td>{html.escape(k)}<br><b>{v}</b></td>" for k, v in stats.items()) + "</tr></table><br>")

    if n == 0:
        parts.append("<p>No new KID documents were found for this period.</p>")
    else:
        if summary_fields:
            parts.append("<table class='grid'><tr><th>#</th>" + "".join(
                f"<th>{html.escape(f['label'])}</th>" for f in summary_fields) + "</tr>")
            for i, r in enumerate(results, 1):
                parts.append(f"<tr><td>{i}</td>" + "".join(
                    f"<td>{_v(r['fields'].get(f['key']))}</td>" for f in summary_fields) + "</tr>")
        else:
            parts.append("<table class='grid'><tr><th>#</th>" + "".join(
                f"<th>{html.escape(c)}</th>" for c, _ in src_cols) + "</tr>")
            for i, r in enumerate(results, 1):
                parts.append(f"<tr><td>{i}</td>" + "".join(
                    f"<td>{_v(get(r['source']))}</td>" for _, get in src_cols) + "</tr>")
        parts.append("</table>")

        # One card per KID with its extracted fields. Without fields (Claude switched off)
        # the summary table above already says everything, so no cards are added.
        for i, r in enumerate(results if fields else [], 1):
            src = r["source"]
            parts.append("<div class='card'>")
            parts.append(f"<h2>{i}. {html.escape(r['fields'].get('issuer_name') or src.get('company', ''))}</h2>")
            if fields:
                parts.append("<table class='kv'>")
                for f in fields:
                    parts.append(f"<tr><td class='k'>{html.escape(f['label'])}</td>"
                                 f"<td>{_v(r['fields'].get(f['key']))}</td></tr>")
                parts.append("</table>")
            notes = r["fields"].get("extraction_notes")
            if notes:
                parts.append(f"<div class='note'><b>Note:</b> {html.escape(notes)}</div>")
            if r["fields"].get("is_kid_document") is False:
                parts.append("<div class='note'><b>Warning:</b> Claude reports this file may not be a KID.</div>")
            parts.append("</div>")

    if failures:
        parts.append("<h1 style='font-size:16px;margin-top:10px'>Items that need attention</h1><div class='fail'><ul>")
        for fl in failures:
            parts.append(f"<li><b>{html.escape(fl.get('item', ''))}</b> — {html.escape(fl.get('error', ''))}</li>")
        parts.append("</ul></div>")

    if fields:
        parts.append("<div class='foot'>Automated report · BSE memorandum data → KID → Claude extraction. "
                     "Values are machine-extracted; verify against the source document before acting on them.</div>")
    else:
        parts.append("<div class='foot'>Automated report · BSE memorandum data → KID files</div>")
    parts.append("</div></body></html>")

    # ---------- plain text
    lines = ["BSE Private Placement — Key Information Documents", f"Allotment date: {period_label}", ""]
    if part_note:
        lines += [part_note, ""]
    lines += [f"{k}: {v}" for k, v in stats.items()] + [""]
    if n == 0:
        lines.append("No new KID documents were found for this period.")
    for i, r in enumerate(results, 1):
        name = r['fields'].get('issuer_name') or r['source'].get('company')
        lines.append(f"{i}. {name}  ({', '.join(r['source'].get('isins', []))})")
        if "zip" in r["source"]:
            lines.append(f"   KID: {r['source'].get('kid_file')} - {_attached_text(r['source'])}")
        for f in fields:
            lines.append(f"   {f['label']}: {r['fields'].get(f['key'])}")
        lines.append("")
    if failures:
        lines.append("Items that need attention:")
        lines += [f" - {fl.get('item')}: {fl.get('error')}" for fl in failures]

    # ---------- CSV
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["BSE Company", "BSE ISINs", "Allotment Date(s)", "KID File", "Download URL"] +
               [f["label"] for f in fields] + ["Notes"])
    for r in results:
        src = r["source"]
        w.writerow([src.get("company"), ", ".join(src.get("isins", [])), ", ".join(src.get("allotment_dates", [])),
                    src.get("kid_file"), src.get("url")] +
                   [r["fields"].get(f["key"]) for f in fields] + [r["fields"].get("extraction_notes", "")])

    subject = f"{period_label} — {n} KID{'s' if n != 1 else ''}"
    if failures:
        subject += f", {len(failures)} issue{'s' if len(failures) != 1 else ''}"
    return {"subject": subject, "html": "".join(parts), "text": "\n".join(lines), "csv": buf.getvalue()}


# ---------------------------------------------------------------- sending
def smtp_login(server: smtplib.SMTP, user: str, password: str, method: str = "") -> None:
    """Log in, optionally forcing one mechanism (LOGIN is what .NET SmtpClient uses)."""
    if not method:
        server.login(user, password)
        return
    server.user, server.password = user, password
    fn = getattr(server, "auth_" + method.lower().replace("-", "_"))
    code, resp = server.auth(method, fn, initial_response_ok=True)
    if code not in (235, 503):
        raise smtplib.SMTPAuthenticationError(code, resp)


def diagnose_smtp(settings: Settings) -> int:
    """Careful SMTP login troubleshooting. Never prints the password.

    1. Checks the stored password for paste problems (control characters, spaces).
    2. Shows what the server supports.
    3. Tries at most 3 username/method combinations (to avoid locking the account)."""
    pw = settings.smtp_password or ""
    user = settings.smtp_username
    src = "SMTP_PASSWORD in .env" if (os.getenv("SMTP_PASSWORD") or "").strip() else "Windows Credential Manager"
    print(f"\nServer   : {settings.smtp_host}:{settings.smtp_port} ({settings.smtp_security})")
    print(f"Username : {user!r}")
    print(f"Password : read from {src}; length {len(pw)} characters")
    problems = []
    if not pw:
        problems.append("no password found - run: .venv\\Scripts\\python.exe -m src.main --set-secret SMTP_PASSWORD")
    if any(ord(c) < 32 for c in pw):
        problems.append("password contains invisible control characters (usually a Ctrl+V that was stored "
                        "instead of the pasted text) - store it again and RIGHT-CLICK to paste, or type it")
    if pw != pw.strip():
        problems.append("password has leading/trailing spaces - store it again")
    if pw[:1] in "\"'" and pw[-1:] in "\"'" and len(pw) > 1:
        problems.append("password is wrapped in quotes - store it again WITHOUT the quotes")
    if problems:
        print("\nPROBLEM with the stored password (no login attempted):")
        for p in problems:
            print("  - " + p)
        return 1
    print("Password : looks clean (no hidden characters/spaces/quotes)")

    def connect() -> smtplib.SMTP:
        if settings.smtp_security == "ssl":
            srv = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30,
                                   context=ssl.create_default_context())
        else:
            srv = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
        srv.ehlo()
        if settings.smtp_security == "starttls":
            srv.starttls(context=ssl.create_default_context())
            srv.ehlo()
        return srv

    srv = connect()
    mechs = (srv.esmtp_features.get("auth") or "").upper().split()
    srv.quit()
    print(f"Server supports login methods: {mechs or 'none advertised'}")

    domain = settings.email_from.split("@")[-1] if "@" in settings.email_from else ""
    users = [user] + ([f"{user}@{domain}"] if domain and "@" not in user else [])
    methods = [m for m in ("LOGIN", "PLAIN") if m in mechs] or ["LOGIN"]
    combos = [(users[0], methods[0])]
    if len(users) > 1:
        combos.append((users[1], methods[0]))
    if len(methods) > 1:
        combos.append((users[0], methods[1]))
    print(f"Trying {len(combos)} combination(s) (limited on purpose, to avoid locking the account)...\n")
    for u, m in combos:
        try:
            srv = connect()
            smtp_login(srv, u, pw, m)
            srv.quit()
            print(f"  SUCCESS  username={u!r}  method={m}")
            print("\nPut these in .env and run --test-email again:")
            print(f"  SMTP_USERNAME={u}")
            print(f"  SMTP_AUTH_METHOD={m}")
            return 0
        except smtplib.SMTPAuthenticationError as exc:
            print(f"  failed   username={u!r}  method={m}: {exc.smtp_code} {exc.smtp_error.decode(errors='ignore')}")
        except Exception as exc:
            print(f"  error    username={u!r}  method={m}: {exc}")
    print("\nAll attempts were rejected by the server, so the username/password pair itself is not accepted.")
    print("Most likely the stored password differs from the real one. Store it again (right-click to paste),")
    print("or confirm with IT that this account may send via SMTP from this PC (some servers restrict by IP).")
    return 1


class EmailService:
    def __init__(self, settings: Settings):
        self.s = settings

    def compose(self, report: Dict[str, Any], csv_name: str,
                attachments: Optional[Sequence[Tuple[Path, str]]] = None) -> EmailMessage:
        msg = EmailMessage(policy=policy.SMTP)
        msg["Subject"] = f"{self.s.email_subject_prefix} | {report['subject']}"
        msg["From"] = formataddr((self.s.email_from_name, self.s.email_from))
        msg["To"] = ", ".join(self.s.email_to)
        if self.s.email_cc:
            msg["Cc"] = ", ".join(self.s.email_cc)
        msg["Message-ID"] = make_msgid(domain=(self.s.email_from.split("@")[-1] or "localhost"))
        msg.set_content(report["text"])
        msg.add_alternative(report["html"], subtype="html")
        if self.s.email_attach_csv and report.get("csv") and report["csv"].count("\n") > 1:
            msg.add_attachment(report["csv"].encode("utf-8-sig"), maintype="text", subtype="csv", filename=csv_name)
        for path, name in attachments or []:
            subtype = "zip" if Path(path).suffix.lower() == ".zip" else "pdf"
            msg.add_attachment(Path(path).read_bytes(), maintype="application", subtype=subtype, filename=name)
        return msg

    def _recipients(self) -> List[str]:
        return list(dict.fromkeys(self.s.email_to + self.s.email_cc + self.s.email_bcc))

    def _smtp_send(self, msg: EmailMessage, recipients: List[str]) -> None:
        timeout = 60
        if self.s.smtp_security == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(self.s.smtp_host, self.s.smtp_port, timeout=timeout,
                                                   context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=timeout)
        try:
            server.ehlo()
            if self.s.smtp_security == "starttls":
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            if self.s.smtp_username:
                smtp_login(server, self.s.smtp_username, self.s.smtp_password or "", self.s.smtp_auth_method)
            refused = server.send_message(msg, from_addr=self.s.email_from, to_addrs=recipients)
            if refused:
                log.warning("Some recipients were refused: %s", refused)
        finally:
            try:
                server.quit()
            except Exception:
                pass

    def send(self, msg: EmailMessage) -> bool:
        """Send with retries. On final failure, save to outbox and return False."""
        recipients = self._recipients()
        if not recipients:
            log.error("Email not sent: no recipients configured (EMAIL_TO)")
            self._save_outbox(msg)
            return False
        last_exc: Exception | None = None
        for attempt in range(1, self.s.email_max_retries + 1):
            try:
                self._smtp_send(msg, recipients)
                log.info("Email sent successfully to %d recipient(s): %s | subject=%r",
                         len(recipients), ", ".join(recipients), msg["Subject"])
                return True
            except smtplib.SMTPAuthenticationError as exc:
                last_exc = exc
                log.error("Email authentication failed (check SMTP_USERNAME / SMTP_PASSWORD / app password): %s", exc)
                break  # retrying will not help
            except (smtplib.SMTPException, OSError) as exc:
                last_exc = exc
                if attempt < self.s.email_max_retries:
                    delay = 10 * attempt
                    log.warning("Email send failed (attempt %d/%d): %s — retrying in %ds",
                                attempt, self.s.email_max_retries, exc, delay)
                    time.sleep(delay)
        log.error("Email delivery FAILED: %s", last_exc)
        self._save_outbox(msg)
        return False

    def _save_outbox(self, msg: EmailMessage) -> Path:
        self.s.outbox_dir.mkdir(parents=True, exist_ok=True)
        path = self.s.outbox_dir / f"pending_{datetime.now().strftime('%Y%m%d_%H%M%S')}.eml"
        path.write_bytes(bytes(msg))
        log.error("Unsent email saved to %s — it will be retried on the next run (or run --resend-pending)", path)
        return path

    def resend_pending(self) -> int:
        """Try to send every .eml in the outbox. Returns number sent."""
        sent = 0
        for path in sorted(self.s.outbox_dir.glob("pending_*.eml")):
            try:
                msg = BytesParser(policy=policy.SMTP).parsebytes(path.read_bytes())
                self._smtp_send(msg, self._recipients())  # type: ignore[arg-type]
                sent_path = path.with_name(path.name.replace("pending_", "sent_"))
                path.rename(sent_path)
                log.info("Re-sent pending email %s", path.name)
                sent += 1
            except Exception as exc:
                log.error("Re-send of %s failed: %s", path.name, exc)
        return sent

    def send_test(self) -> bool:
        report = {
            "subject": "Test email",
            "html": "<p>This is a test email from <b>bse-kid-scraper</b>. SMTP settings are working.</p>",
            "text": "This is a test email from bse-kid-scraper. SMTP settings are working.",
            "csv": "",
        }
        msg = self.compose(report, "test.csv")
        ok = False
        try:
            self._smtp_send(msg, self._recipients())
            ok = True
            log.info("Test email sent to %s", ", ".join(self._recipients()))
        except Exception as exc:
            log.error("Test email failed: %s", exc)
        return ok
