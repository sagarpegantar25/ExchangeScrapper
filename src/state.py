"""
SQLite-backed state store — the heart of duplicate prevention.

Tables
------
documents : one row per download URL (BSE or NSE; see the exchange column) (a ZIP can cover several ISINs).
kids      : one row per unique KID PDF, keyed by SHA-256 of its bytes, so the
            same KID appearing in two ZIPs (or re-uploaded) is sent to Claude once.
runs      : one row per pipeline run, for auditing.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    url             TEXT PRIMARY KEY,
    company         TEXT,
    isins           TEXT,
    allotment_date  TEXT,
    status          TEXT NOT NULL DEFAULT 'new',   -- new | downloaded | done | no_kid | failed
    zip_path        TEXT,
    zip_sha256      TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    first_seen      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kids (
    sha256          TEXT PRIMARY KEY,
    doc_url         TEXT,
    file_name       TEXT,
    archive_path    TEXT,
    status          TEXT NOT NULL DEFAULT 'pending', -- pending | extracted | failed
    result_json     TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    emailed_at      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    date_from       TEXT,
    date_to         TEXT,
    status          TEXT,
    summary         TEXT
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class StateStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        # Migration: databases from bse-kid-scraper have no "exchange" column; their rows are BSE.
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(documents)")}
        if "exchange" not in cols:
            self.conn.execute("ALTER TABLE documents ADD COLUMN exchange TEXT NOT NULL DEFAULT 'BSE'")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ------------------------------------------------------------- documents
    def get_document(self, url: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM documents WHERE url=?", (url,)).fetchone()

    def upsert_document(self, url: str, company: str, isins: List[str], allotment_date: str,
                        exchange: str = "BSE") -> None:
        now = _now()
        with self.tx() as c:
            c.execute(
                """INSERT INTO documents(url, company, isins, allotment_date, first_seen, updated_at, exchange)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(url) DO UPDATE SET company=excluded.company, isins=excluded.isins,
                       allotment_date=excluded.allotment_date, updated_at=excluded.updated_at,
                       exchange=excluded.exchange""",
                (url, company, json.dumps(isins), allotment_date, now, now, exchange),
            )

    def update_document(self, url: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE documents SET {cols} WHERE url=?", (*fields.values(), url))

    def bump_document_attempt(self, url: str, error: str) -> int:
        with self.tx() as c:
            c.execute(
                "UPDATE documents SET attempts=attempts+1, last_error=?, status='failed', updated_at=? WHERE url=?",
                (error[:2000], _now(), url),
            )
        row = self.get_document(url)
        return int(row["attempts"]) if row else 0

    def should_skip_document(self, url: str, max_attempts: int) -> Optional[str]:
        """Return a reason string if this document should NOT be processed again."""
        row = self.get_document(url)
        if row is None:
            return None
        if row["status"] in ("done", "no_kid"):
            return f"already processed (status={row['status']})"
        if row["status"] == "failed" and row["attempts"] >= max_attempts:
            return f"gave up after {row['attempts']} failed attempts"
        return None

    # ------------------------------------------------------------- kids
    def get_kid(self, sha256: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM kids WHERE sha256=?", (sha256,)).fetchone()

    def register_kid(self, sha256: str, doc_url: str, file_name: str, archive_path: str) -> None:
        now = _now()
        with self.tx() as c:
            c.execute(
                """INSERT INTO kids(sha256, doc_url, file_name, archive_path, created_at, updated_at)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(sha256) DO NOTHING""",
                (sha256, doc_url, file_name, archive_path, now, now),
            )

    def save_kid_result(self, sha256: str, result: Dict[str, Any]) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE kids SET status='extracted', result_json=?, last_error=NULL, updated_at=? WHERE sha256=?",
                (json.dumps(result, ensure_ascii=False), _now(), sha256),
            )

    def fail_kid(self, sha256: str, error: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE kids SET status='failed', attempts=attempts+1, last_error=?, updated_at=? WHERE sha256=?",
                (error[:2000], _now(), sha256),
            )

    def mark_kids_emailed(self, sha_list: List[str]) -> None:
        now = _now()
        with self.tx() as c:
            c.executemany("UPDATE kids SET emailed_at=? WHERE sha256=?", [(now, s) for s in sha_list])

    def unemailed_results(self) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM kids WHERE status='extracted' AND emailed_at IS NULL ORDER BY created_at"
        ).fetchall()

    # ------------------------------------------------------------- runs
    def start_run(self, date_from: str, date_to: str) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO runs(started_at, date_from, date_to, status) VALUES(?,?,?, 'running')",
                (_now(), date_from, date_to),
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, summary: Dict[str, Any]) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE runs SET finished_at=?, status=?, summary=? WHERE id=?",
                (_now(), status, json.dumps(summary), run_id),
            )

    def retryable_documents(self, max_attempts: int, max_age_days: int = 7,
                            exchange: str = "BSE") -> List[sqlite3.Row]:
        """Documents of this exchange from recent runs that failed/stalled and still have attempts left."""
        return self.conn.execute(
            """SELECT * FROM documents
               WHERE status IN ('new','downloaded','failed') AND attempts < ? AND exchange = ?
                 AND julianday('now','localtime') - julianday(first_seen) <= ?
               ORDER BY first_seen""",
            (max_attempts, exchange, max_age_days),
        ).fetchall()

    def reset_document(self, url: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM documents WHERE url=?", (url,))
