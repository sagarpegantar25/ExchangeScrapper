"""
Recursive ZIP extraction and KID discovery.

Real BSE packages look like:
    PPDIDisclosureDocuments_20260929174501.zip
      ├── CMES GID NEW_20260929161610.zip      (≈130 MB, nested)
      │     ├── 1.Kid.pdf                      <-- what we want
      │     └── 2.GID.pdf ...
      ├── Credit rating_20260925145122.pdf
      └── DT Consent_20260925145122.pdf

Strategy
  1. Walk every ZIP recursively (depth-limited). Every member is extracted to disk
     (streamed, not held in RAM); nested ZIPs are walked in turn.
  2. PDFs whose file name matches a KID pattern are the KIDs.
  3. If a package yields no KID by name, optionally scan the first pages of every
     PDF in it for "Key Information Document" (KID_CONTENT_FALLBACK).
  4. Every KID is hashed (SHA-256) and copied next to the package:
        <day>/<Company>_<ISIN>/<name>.pdf

Safety: zip-slip protection, depth limit, total extracted size limit,
tolerance for corrupt members (one bad file never stops the others).
"""
from __future__ import annotations

import logging
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import List

from .config import Settings
from .utils import safe_name, sha256_file

log = logging.getLogger("bse_kid.zip")


@dataclass
class KidFile:
    path: Path            # archived copy
    original_name: str    # name inside the zip
    nesting: str          # e.g. "Main.zip > Inner.zip > 1.Kid.pdf"
    sha256: str
    size: int
    matched_by: str       # "filename" | "content"


class ZipLimitError(Exception):
    pass


def _decode_member_name(info: zipfile.ZipInfo) -> str:
    """Zip files created on Windows without the UTF-8 flag use cp437; try to recover."""
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        return name.encode("cp437").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


class ZipProcessor:
    def __init__(self, settings: Settings):
        self.s = settings
        self.patterns = [re.compile(p, re.IGNORECASE) for p in settings.kid_filename_patterns]
        self._budget = 0

    # ----------------------------------------------------------- public
    def is_kid_name(self, name: str) -> bool:
        base = PurePosixPath(name).name
        if not base.lower().endswith(".pdf"):
            return False
        stem = base[:-4]
        return any(p.search(stem) for p in self.patterns)

    def process(self, package: Path, work_dir: Path, archive_dir: Path, label: str) -> List[KidFile]:
        """Extract `package` (zip or pdf) under work_dir and archive any KIDs found."""
        work_dir.mkdir(parents=True, exist_ok=True)
        self._budget = self.s.max_extracted_mb * 1024 * 1024
        found: List[tuple] = []        # (extracted_path, member_name, nesting, matched_by)
        all_pdfs: List[tuple] = []     # (extracted_or_None, zip_path, member_name, nesting)

        if package.suffix.lower() == ".pdf":
            if self.is_kid_name(package.name):
                found.append((package, package.name, package.name, "filename"))
            else:
                all_pdfs.append((package, None, package.name, package.name))
        elif zipfile.is_zipfile(package):
            log.info("ZIP extraction started: %s", package.name)
            self._walk(package, work_dir, package.name, 0, found, all_pdfs)
        else:
            # Treated as a failure (not "no KID") so the document is retried next run.
            raise ValueError(f"Downloaded file is neither a valid ZIP nor a PDF: {package.name}")

        if not found and self.s.kid_content_fallback and all_pdfs:
            log.info("No KID by file name in %s — scanning %d PDF(s) for 'Key Information Document'",
                     package.name, len(all_pdfs))
            found.extend(self._content_fallback(all_pdfs, work_dir))

        kids: List[KidFile] = []
        seen = set()
        for src, member, nesting, how in found:
            try:
                digest = sha256_file(src)
                if digest in seen:
                    continue
                seen.add(digest)
                archive_dir.mkdir(parents=True, exist_ok=True)
                base = PurePosixPath(member).name
                dest = archive_dir / safe_name(f"{label}_{base}" if label else base)
                n = 2
                while dest.exists() and sha256_file(dest) != digest:   # same name, different KID
                    dest = dest.with_name(f"{Path(base).stem}_{n}.pdf")
                    n += 1
                if not dest.exists():
                    shutil.copy2(src, dest)
                kids.append(KidFile(dest, PurePosixPath(member).name, nesting, digest, dest.stat().st_size, how))
                log.info("KID PDF found: %s  [%s, %.1f MB, via %s]", nesting, digest[:12],
                         dest.stat().st_size / 1e6, how)
            except Exception as exc:
                log.error("Could not archive KID %s: %s", nesting, exc)
        if not kids:
            log.warning("No KID PDF found in %s", package.name)
        return kids

    # ----------------------------------------------------------- internals
    def _safe_target(self, root: Path, member: str) -> Path:
        parts = [safe_name(p) for p in PurePosixPath(member.replace("\\", "/")).parts
                 if p not in ("", ".", "..", "/")]
        target = root.joinpath(*parts) if parts else root / "unnamed"
        if not str(target.resolve()).startswith(str(root.resolve())):
            raise ZipLimitError(f"Unsafe path in zip: {member}")
        return target

    def _extract_member(self, zf: zipfile.ZipFile, info: zipfile.ZipInfo, target: Path) -> Path:
        if info.file_size > self._budget:
            raise ZipLimitError(f"Extraction budget exceeded (MAX_EXTRACTED_MB) at {info.filename}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        self._budget -= info.file_size
        return target

    def _walk(self, zip_path: Path, out_dir: Path, nesting: str, depth: int,
              found: list, all_pdfs: list) -> None:
        if depth > self.s.max_zip_depth:
            log.warning("Max ZIP depth (%d) reached at %s — not going deeper", self.s.max_zip_depth, nesting)
            return
        try:
            zf = zipfile.ZipFile(zip_path)
        except (zipfile.BadZipFile, OSError) as exc:
            log.error("Corrupt/unreadable ZIP %s: %s", nesting, exc)
            return

        with zf:
            members = [i for i in zf.infolist() if not i.is_dir()]
            log.info("  %s%s: %d file(s)", "  " * depth, nesting, len(members))
            for info in members:
                name = _decode_member_name(info)
                lower = name.lower()
                if "__macosx/" in lower or PurePosixPath(lower).name.startswith("._"):
                    continue   # macOS metadata, not real files (e.g. __MACOSX/._KID.pdf)
                child_nesting = f"{nesting} > {name}"
                try:
                    if lower.endswith(".zip"):
                        target = self._safe_target(out_dir, name)
                        self._extract_member(zf, info, target)
                        if zipfile.is_zipfile(target):
                            sub_dir = target.with_name(target.stem + "_unzipped")
                            self._walk(target, sub_dir, child_nesting, depth + 1, found, all_pdfs)
                        else:
                            log.warning("Nested file named .zip is not a valid zip: %s", child_nesting)
                    else:
                        # Everything is extracted, so the unzipped folder mirrors the package.
                        target = self._safe_target(out_dir, name)
                        self._extract_member(zf, info, target)
                        if lower.endswith(".pdf"):
                            if self.is_kid_name(name):
                                found.append((target, name, child_nesting, "filename"))
                            else:
                                all_pdfs.append((target, zip_path, info, child_nesting))
                        elif lower.endswith((".rar", ".7z")):
                            log.warning("Unsupported archive type, left packed: %s", child_nesting)
                except ZipLimitError:
                    raise
                except Exception as exc:
                    log.error("Failed to process member %s: %s", child_nesting, exc)

    def _content_fallback(self, all_pdfs: list, work_dir: Path) -> List[tuple]:
        from pypdf import PdfReader

        hits = []
        scan_dir = work_dir / "_content_scan"
        for extracted, zip_path, info, nesting in all_pdfs:
            try:
                if extracted is None:
                    if info.file_size > 60 * 1024 * 1024:   # huge PDFs are GIDs, not KIDs
                        continue
                    with zipfile.ZipFile(zip_path) as zf:
                        name = _decode_member_name(info)
                        extracted = self._extract_member(zf, info, self._safe_target(scan_dir, name))
                reader = PdfReader(str(extracted))
                if reader.is_encrypted:
                    try:
                        reader.decrypt("")
                    except Exception:
                        continue
                text = ""
                for page in reader.pages[:3]:
                    text += (page.extract_text() or "") + "\n"
                t = re.sub(r"\s+", " ", text.lower())
                if "key information document" in t:
                    member = nesting.split(" > ")[-1]
                    hits.append((extracted, member, nesting, "content"))
            except ZipLimitError:
                raise
            except Exception as exc:
                log.debug("Content scan failed for %s: %s", nesting, exc)
        return hits
