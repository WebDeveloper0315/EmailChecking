"""Saving attachments to disk and exposing inline images to the HTML view."""

from __future__ import annotations

import base64
import logging
import os
import re
from pathlib import Path
from typing import Callable, Iterable, Optional

from mail_parser import find_inline_attachment
from models import Attachment, Email

logger = logging.getLogger(__name__)

__all__ = [
    "human_size",
    "sanitize_filename",
    "save_attachment",
    "save_all",
    "data_uri",
    "make_data_uri_resolver",
]

#: Inline images larger than this are not embedded as data URIs (a data URI is
#: ~33% bigger than the payload and the whole document is held in memory).
MAX_INLINE_BYTES = 8 * 1024 * 1024

_ILLEGAL = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
#: Names Windows refuses regardless of extension.
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def human_size(num_bytes: int) -> str:
    """``1536`` -> ``1.5 KB``."""
    size = float(max(0, num_bytes))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"  # pragma: no cover


def sanitize_filename(name: str, fallback: str = "attachment.bin") -> str:
    """Make an attachment name safe to write on this platform.

    Path separators, directory traversal, control characters and reserved
    Windows device names are all neutralised.
    """
    candidate = (name or "").strip().replace("\r", "").replace("\n", "")
    # Drop any directory part first - senders do put "../../etc/passwd" in there.
    candidate = candidate.replace("\\", "/").rsplit("/", 1)[-1]
    candidate = os.path.basename(candidate)
    candidate = _ILLEGAL.sub("_", candidate).strip(" .")
    if not candidate or set(candidate) <= {"_"}:
        return fallback
    stem, dot, extension = candidate.partition(".")
    if stem.lower() in _RESERVED:
        candidate = f"_{candidate}"
    if len(candidate) > 200:  # keep well below MAX_PATH / ext4 limits
        stem, dot, extension = candidate.rpartition(".")
        candidate = (stem[:190] + dot + extension[:10]) if dot else candidate[:200]
    return candidate


def unique_destination(directory: Path, filename: str) -> Path:
    """``report.pdf`` -> ``report (2).pdf`` when the name is taken."""
    target = directory / filename
    if not target.exists():
        return target
    stem, dot, extension = filename.rpartition(".")
    if not dot:
        stem, extension = filename, ""
    counter = 2
    while True:
        suffix = f".{extension}" if extension else ""
        candidate = directory / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def save_attachment(attachment: Attachment, directory: str | Path) -> Path:
    """Write one attachment, returning the path actually used."""
    folder = Path(directory).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    destination = unique_destination(folder, sanitize_filename(attachment.filename))
    data = attachment.data
    destination.write_bytes(data)
    logger.info("Saved %s (%s) to %s", attachment.filename, human_size(len(data)), destination)
    return destination


def save_all(attachments: Iterable[Attachment], directory: str | Path) -> list[tuple[Attachment, Optional[Path], str]]:
    """Save several attachments.

    Returns one ``(attachment, path, error)`` row per input so that a single
    failure never hides the successes.
    """
    results: list[tuple[Attachment, Optional[Path], str]] = []
    for attachment in attachments:
        try:
            results.append((attachment, save_attachment(attachment, directory), ""))
        except Exception as exc:
            logger.exception("Could not save %s", attachment.filename)
            results.append((attachment, None, str(exc)))
    return results


# ------------------------------------------------------------- inline images
def data_uri(attachment: Attachment, max_bytes: int = MAX_INLINE_BYTES) -> Optional[str]:
    """``data:`` URI for an inline image, or ``None`` if it is too big/broken."""
    if attachment.size and attachment.size > max_bytes:
        logger.debug("Inline image %s too large (%s)", attachment.filename,
                     human_size(attachment.size))
        return None
    try:
        payload = attachment.data
    except Exception:
        logger.debug("Inline image %s could not be decoded", attachment.filename, exc_info=True)
        return None
    if not payload or len(payload) > max_bytes:
        return None
    mime = attachment.content_type or "application/octet-stream"
    if not mime.startswith("image/"):
        return None
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def make_data_uri_resolver(
    mail: Email, max_bytes: int = MAX_INLINE_BYTES
) -> Callable[[str], Optional[str]]:
    """CID resolver for viewers that understand ``data:`` URIs (QWebEngineView).

    Results are memoised: the same image referenced ten times is decoded once.
    """
    cache: dict[str, Optional[str]] = {}

    def resolve(cid: str) -> Optional[str]:
        if cid not in cache:
            attachment = find_inline_attachment(mail, cid)
            cache[cid] = data_uri(attachment, max_bytes) if attachment else None
        return cache[cid]

    return resolve


def make_passthrough_resolver(mail: Email) -> Callable[[str], Optional[str]]:
    """CID resolver for QTextBrowser, which resolves ``cid:`` itself.

    The URL is kept as-is when the message really contains that part, so the
    text browser can ask us for it later through ``loadResource``.
    """

    def resolve(cid: str) -> Optional[str]:
        attachment = find_inline_attachment(mail, cid)
        if attachment is None or not attachment.is_image:
            return None
        return f"cid:{cid}"

    return resolve
