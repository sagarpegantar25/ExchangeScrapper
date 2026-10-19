"""
Send each KID PDF to the Claude API and get back a fixed, predefined structure.

* The PDF is sent as a base64 `document` block (Claude reads text + layout).
* Output is forced through a tool ("record_kid_data") whose JSON schema is built
  from config/kid_fields.json, so every response has exactly the same keys.
* Values are then normalised (dates → DD-MM-YYYY, blanks → "Not found").
* Retries with exponential backoff on 429 (rate limit), 5xx/529 (overloaded),
  timeouts and connection errors; honours the `retry-after` header.
* A minimum interval between requests keeps us under rate limits.
* Oversized PDFs are trimmed to the first CLAUDE_MAX_PDF_PAGES pages
  (a KID's term sheet is at the front).
"""
from __future__ import annotations

import base64
import io
import logging
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import Settings

log = logging.getLogger("exch.claude")

NOT_FOUND = "Not found"
TOOL_NAME = "record_kid_data"

SYSTEM_PROMPT = (
    "You are a meticulous fixed-income analyst in India. You read Key Information "
    "Documents (KIDs) for privately placed debt securities listed on BSE or NSE and extract "
    "facts exactly as stated in the document. Never guess or infer values that are not "
    "in the document; use an empty string when a value is absent. When a document covers "
    "several series/options, include each series' value separated by ' | ' and prefix "
    "it with the series name. Keep amounts with their units (crore, lakh, INR)."
)


class ClaudeError(Exception):
    pass


class ClaudeProcessor:
    def __init__(self, settings: Settings, client: Any = None):
        self.s = settings
        self.fields: List[Dict[str, Any]] = settings.load_fields()
        self._last_call = 0.0
        if client is not None:
            self.client = client
        else:
            if not settings.anthropic_api_key:
                raise ClaudeError("ANTHROPIC_API_KEY is not configured")
            import anthropic

            # We do our own retry loop (with logging), so SDK retries are off.
            self.client = anthropic.Anthropic(
                api_key=settings.anthropic_api_key,
                timeout=float(settings.claude_timeout),
                max_retries=0,
            )

    # ------------------------------------------------------------- schema
    def tool_schema(self) -> Dict[str, Any]:
        props: Dict[str, Any] = {}
        for f in self.fields:
            desc = f.get("description", f["label"])
            if f.get("type") == "list":
                props[f["key"]] = {"type": "array", "items": {"type": "string"}, "description": desc}
            elif f.get("type") == "date":
                props[f["key"]] = {"type": "string", "description": desc + " (format DD-MM-YYYY if possible)"}
            else:
                props[f["key"]] = {"type": "string", "description": desc}
        props["extraction_notes"] = {
            "type": "string",
            "description": "Anything ambiguous, conflicting or noteworthy about this extraction (short).",
        }
        props["is_kid_document"] = {
            "type": "boolean",
            "description": "True if this really is a Key Information Document for a debt issue.",
        }
        return {
            "name": TOOL_NAME,
            "description": "Record the structured data extracted from the Key Information Document.",
            "input_schema": {
                "type": "object",
                "properties": props,
                "required": [f["key"] for f in self.fields] + ["is_kid_document"],
            },
        }

    def _user_prompt(self, context: Dict[str, Any]) -> str:
        lines = [
            "Extract the fields defined in the tool from the attached Key Information Document.",
            "Call the tool exactly once with all fields. Use '' for anything not stated.",
        ]
        if context:
            lines.append("Context from the exchange listing (for cross-checking only; prefer the document):")
            for k, v in context.items():
                lines.append(f"- {k}: {v}")
        return "\n".join(lines)

    # ------------------------------------------------------------- PDF prep
    def _prepare_pdf(self, pdf_path: Path, max_pages: Optional[int] = None) -> Tuple[bytes, str]:
        raw = pdf_path.read_bytes()
        max_pages = max_pages or self.s.claude_max_pdf_pages
        max_bytes = int(self.s.claude_max_pdf_mb * 1024 * 1024)
        try:
            from pypdf import PdfReader, PdfWriter

            reader = PdfReader(io.BytesIO(raw))
            if reader.is_encrypted:
                reader.decrypt("")
            n = len(reader.pages)
        except Exception as exc:
            log.warning("pypdf could not open %s (%s) — sending as-is", pdf_path.name, exc)
            return raw, "original (unparsed)"

        if n <= max_pages and len(raw) <= max_bytes and not reader.is_encrypted:
            return raw, f"{n} pages"

        pages = min(n, max_pages)
        while pages >= 1:
            writer = PdfWriter()
            for i in range(pages):
                writer.add_page(reader.pages[i])
            buf = io.BytesIO()
            writer.write(buf)
            data = buf.getvalue()
            if len(data) <= max_bytes:
                log.info("Trimmed %s from %d to %d pages (%.1f MB) for the API", pdf_path.name, n, pages, len(data) / 1e6)
                return data, f"first {pages} of {n} pages"
            pages //= 2
        raise ClaudeError(f"{pdf_path.name} is too large to send even after trimming")

    # ------------------------------------------------------------- API call
    def _pace(self) -> None:
        wait = self.s.claude_min_interval_sec - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def _call_api(self, pdf_bytes: bytes, prompt: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        import anthropic

        retry_status = {408, 409, 429, 500, 502, 503, 504, 529}
        b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")
        attempts = self.s.claude_max_retries
        for attempt in range(1, attempts + 1):
            self._pace()
            started = time.time()
            try:
                log.info("Claude API request (model=%s, attempt %d/%d, %.1f MB PDF)",
                         self.s.claude_model, attempt, attempts, len(pdf_bytes) / 1e6)
                resp = self.client.messages.create(
                    model=self.s.claude_model,
                    max_tokens=self.s.claude_max_tokens,
                    system=SYSTEM_PROMPT,
                    tools=[self.tool_schema()],
                    tool_choice={"type": "tool", "name": TOOL_NAME},
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "document",
                             "source": {"type": "base64", "media_type": "application/pdf", "data": b64}},
                            {"type": "text", "text": prompt},
                        ],
                    }],
                )
                usage = getattr(resp, "usage", None)
                meta = {
                    "model": getattr(resp, "model", self.s.claude_model),
                    "input_tokens": getattr(usage, "input_tokens", None),
                    "output_tokens": getattr(usage, "output_tokens", None),
                    "stop_reason": getattr(resp, "stop_reason", None),
                    "seconds": round(time.time() - started, 1),
                }
                log.info("Claude API response: %s", meta)
                for block in resp.content:
                    if getattr(block, "type", None) == "tool_use" and block.name == TOOL_NAME:
                        return dict(block.input), meta
                raise ClaudeError(f"No tool output in response (stop_reason={meta['stop_reason']})")
            except (anthropic.APIConnectionError, anthropic.APIStatusError) as exc:
                status = getattr(exc, "status_code", None)
                if isinstance(exc, anthropic.APIStatusError) and status not in retry_status:
                    # 400/401/403/404/413: not worth retrying as-is
                    raise ClaudeError(f"Claude API error {status}: {getattr(exc, 'message', exc)}") from exc
                if attempt >= attempts:
                    raise ClaudeError(f"Claude API failed after {attempts} attempts: {exc}") from exc
                delay = self._retry_delay(exc, attempt)
                log.warning("Claude API %s (attempt %d/%d) — retrying in %.0fs",
                            type(exc).__name__, attempt, attempts, delay)
                time.sleep(delay)
        raise ClaudeError("unreachable")

    @staticmethod
    def _retry_delay(exc: Exception, attempt: int) -> float:
        resp = getattr(exc, "response", None)
        if resp is not None:
            ra = resp.headers.get("retry-after")
            if ra:
                try:
                    return min(120.0, float(ra)) + random.uniform(0, 2)
                except ValueError:
                    pass
        return min(120.0, 5 * (2 ** (attempt - 1))) + random.uniform(0, 3)

    # ------------------------------------------------------------- public
    def extract(self, pdf_path: Path, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Return {'fields': {...normalised...}, 'meta': {...}} for one KID."""
        context = context or {}
        pdf_bytes, pages_note = self._prepare_pdf(pdf_path)
        prompt = self._user_prompt(context)
        try:
            raw, meta = self._call_api(pdf_bytes, prompt)
        except ClaudeError as exc:
            # A 400 about size/pages: retry once with a much smaller slice.
            cause = exc.__cause__
            if (getattr(cause, "status_code", None) in (400, 413)
                    and re.search(r"page|size|large|token|limit", str(cause), re.I)):
                log.warning("Request too large for %s — retrying with first 30 pages", pdf_path.name)
                pdf_bytes, pages_note = self._prepare_pdf(pdf_path, max_pages=30)
                raw, meta = self._call_api(pdf_bytes, prompt)
            else:
                raise
        meta["pages_sent"] = pages_note
        return {"fields": self.normalise(raw), "meta": meta}

    def normalise(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for f in self.fields:
            key, ftype = f["key"], f.get("type", "string")
            val = raw.get(key)
            if ftype == "list":
                if isinstance(val, str):
                    val = [v.strip() for v in re.split(r"[,;|\n]", val) if v.strip()]
                val = [str(v).strip() for v in (val or []) if str(v).strip()]
                out[key] = ", ".join(dict.fromkeys(val)) if val else NOT_FOUND
            elif ftype == "date":
                out[key] = normalise_date(val)
            else:
                s = re.sub(r"\s+", " ", str(val)).strip() if val not in (None, "") else ""
                out[key] = s if s and s.lower() not in {"n/a", "na", "none stated", "not available", "null"} else NOT_FOUND
        out["extraction_notes"] = (raw.get("extraction_notes") or "").strip()
        out["is_kid_document"] = bool(raw.get("is_kid_document", True))
        return out

    def ping(self) -> str:
        """Tiny request used by --check to validate the API key and model."""
        resp = self.client.messages.create(
            model=self.s.claude_model, max_tokens=5,
            messages=[{"role": "user", "content": "Reply with OK"}],
        )
        return "".join(getattr(b, "text", "") for b in resp.content).strip()


_DATE_FORMATS = ["%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%d", "%d %B %Y", "%d %b %Y",
                 "%B %d, %Y", "%b %d, %Y", "%d-%b-%Y", "%d-%B-%Y", "%d %b, %Y", "%d %B, %Y"]


def normalise_date(val: Any) -> str:
    if val in (None, ""):
        return NOT_FOUND
    s = str(val).strip()
    cleaned = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s, flags=re.I).replace("  ", " ")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).strftime("%d-%m-%Y")
        except ValueError:
            continue
    return s  # keep original text (e.g. "T+1", "As per bidding") rather than lose it
