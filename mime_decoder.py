"""Low level decoding: charsets, transfer encodings, RFC 2047 headers, dates.

Everything in this module follows one rule: *never raise*.  Real world mail is
full of lies (a Shift-JIS body announced as us-ascii, a charset called
"utf8888", headers with raw 8-bit bytes).  Each helper degrades gracefully and
reports what it had to guess through the returned metadata instead of throwing.
"""

from __future__ import annotations

import binascii
import email.header
import email.utils
import logging
import quopri
from datetime import datetime, timezone
from email.message import Message
from typing import Iterable, Optional, Sequence

from models import Address

logger = logging.getLogger(__name__)

__all__ = [
    "decode_bytes",
    "decode_header_value",
    "decode_addresses",
    "decode_part_payload",
    "decode_part_text",
    "estimate_part_size",
    "normalize_charset",
    "parse_date",
]

#: Charset labels that Python does not know, mapped to the codec that real
#: mail clients use for them.  Keys are compared lower-cased and stripped.
CHARSET_ALIASES: dict[str, str] = {
    "": "utf-8",
    "unknown": "utf-8",
    "unknown-8bit": "utf-8",
    "x-unknown": "utf-8",
    "none": "utf-8",
    "default": "utf-8",
    "ansi_x3.4-1968": "ascii",
    "utf8": "utf-8",
    "utf-8n": "utf-8",
    "utf8mb4": "utf-8",
    "utf-16le": "utf-16-le",
    "utf-16be": "utf-16-be",
    "unicode-1-1-utf-7": "utf-7",
    "unicode-1-1-utf-8": "utf-8",
    # Chinese: gb2312/gbk documents very often contain GB18030-only characters.
    "gb2312": "gb18030",
    "gb_2312-80": "gb18030",
    "gbk": "gb18030",
    "cn-gb": "gb18030",
    "chinese": "gb18030",
    "csgb2312": "gb18030",
    "x-gbk": "gb18030",
    "big5-hkscs": "big5hkscs",
    "cn-big5": "big5",
    "csbig5": "big5",
    # Korean: the classic Microsoft label for CP949.
    "ks_c_5601-1987": "cp949",
    "ks_c_5601-1989": "cp949",
    "ksc5601": "cp949",
    "ks_c_5601": "cp949",
    "euckr": "euc-kr",
    "korean": "cp949",
    # Japanese.
    "shift_jis": "cp932",
    "shift-jis": "cp932",
    "sjis": "cp932",
    "x-sjis": "cp932",
    "iso-2022-jp-2": "iso2022_jp_2",
    "eucjp": "euc-jp",
    "x-euc-jp": "euc-jp",
    # Latin / Cyrillic / Hebrew oddities.
    "iso-8859-8-i": "iso-8859-8",
    "iso-8859-8-e": "iso-8859-8",
    "iso8859-1": "iso-8859-1",
    "latin1": "iso-8859-1",
    "windows-874": "cp874",
    "cp-850": "cp850",
    "ms949": "cp949",
    "x-user-defined": "iso-8859-1",
}

#: Tried in order when the declared charset is missing or wrong.  ``latin-1``
#: is last because it decodes any byte sequence and therefore always "wins".
FALLBACK_CHARSETS: tuple[str, ...] = ("utf-8", "cp1252", "iso-8859-1")

#: Byte order marks that override whatever the headers claim.
#: The codecs are the BOM-aware ones ("utf-16", not "utf-16-le") so that the
#: mark itself is consumed instead of showing up as U+FEFF in the text.
_BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)


def normalize_charset(name: Optional[str]) -> str:
    """Map a charset label found in a mail to a codec Python can use."""
    if not name:
        return "utf-8"
    cleaned = str(name).strip().strip("\"'").lower()
    # Some senders write `charset="utf-8"; format=flowed` into the parameter.
    cleaned = cleaned.split(";")[0].strip()
    if cleaned in CHARSET_ALIASES:
        return CHARSET_ALIASES[cleaned]
    try:
        import codecs

        codecs.lookup(cleaned)
        return cleaned
    except (LookupError, TypeError, ValueError):
        return ""


def _sniff_bom(raw: bytes) -> Optional[str]:
    for bom, codec in _BOMS:
        if raw.startswith(bom):
            return codec
    return None


def _detect_charset(raw: bytes) -> Optional[str]:
    """Statistical detection - only used when the declared charset failed.

    ``charset-normalizer`` / ``chardet`` are optional; without them we simply
    fall back to the static candidate list.
    """
    sample = raw[:64 * 1024]
    try:
        from charset_normalizer import from_bytes  # type: ignore

        best = from_bytes(sample).best()
        if best is not None and best.encoding:
            return normalize_charset(best.encoding) or None
    except Exception:
        pass
    try:
        import chardet  # type: ignore

        guess = chardet.detect(sample)
        if guess and guess.get("encoding") and (guess.get("confidence") or 0) >= 0.6:
            return normalize_charset(guess["encoding"]) or None
    except Exception:
        pass
    return None


def decode_bytes(
    raw: bytes,
    declared: Optional[str] = None,
    extra_candidates: Iterable[str] = (),
) -> tuple[str, str]:
    """Decode ``raw`` to text.

    Returns ``(text, charset_actually_used)``.  The last resort decodes as
    latin-1 with replacement, so this function cannot fail.
    """
    if not raw:
        return "", normalize_charset(declared) or "utf-8"
    if isinstance(raw, str):  # tolerate callers that already have text
        return raw, "utf-8"

    candidates: list[str] = []

    bom = _sniff_bom(raw)
    if bom:
        candidates.append(bom)

    normalized = normalize_charset(declared)
    if normalized:
        candidates.append(normalized)
    elif declared:
        logger.debug("Unknown charset %r, falling back to detection", declared)

    candidates.extend(c for c in extra_candidates if c)

    detected = _detect_charset(raw)
    if detected:
        candidates.append(detected)

    candidates.extend(FALLBACK_CHARSETS)

    seen: set[str] = set()
    for codec in candidates:
        if not codec or codec in seen:
            continue
        seen.add(codec)
        try:
            return raw.decode(codec).lstrip("﻿"), codec
        except (UnicodeDecodeError, LookupError, ValueError):
            continue

    # Cannot happen (latin-1 never fails) but keeps the contract explicit.
    return raw.decode("utf-8", errors="replace"), "utf-8"


def decode_header_value(value: object) -> str:
    """Decode an RFC 2047 header (``=?UTF-8?B?...?=``) into plain text."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        value, _ = decode_bytes(value)
    text = str(value)
    if not text:
        return ""

    try:
        chunks = email.header.decode_header(text)
    except Exception:
        logger.debug("decode_header failed for %r", text[:80], exc_info=True)
        return _unfold(text)

    pieces: list[str] = []
    for chunk, charset in chunks:
        if isinstance(chunk, bytes):
            # An unknown charset here is common; decode_bytes sorts it out.
            decoded, _ = decode_bytes(chunk, charset)
            pieces.append(decoded)
        else:
            pieces.append(chunk)
    return _unfold("".join(pieces))


def _unfold(text: str) -> str:
    """Undo header folding and drop control characters."""
    text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    text = "".join(ch for ch in text if ch >= " " or ch == "\t")
    return " ".join(text.split()) if "  " in text else text.strip()


def decode_addresses(values: object) -> list[Address]:
    """Parse an address header into :class:`Address` objects.

    ``values`` may be a single header string or a list of them (a message can
    legitimately carry several ``Cc`` headers).
    """
    if values is None:
        return []
    if isinstance(values, (list, tuple)):
        raw_values = [str(v) for v in values if v is not None]
    else:
        raw_values = [str(values)]

    result: list[Address] = []
    for raw in raw_values:
        try:
            pairs = email.utils.getaddresses([raw])
        except Exception:
            logger.debug("getaddresses failed for %r", raw[:120], exc_info=True)
            pairs = [("", raw)]
        for name, addr in pairs:
            name = decode_header_value(name).strip().strip('"')
            addr = decode_header_value(addr).strip().strip("<>").strip()
            if not name and not addr:
                continue
            # "Foo Bar" with no <...> is parsed as an address by getaddresses.
            if addr and "@" not in addr and not name:
                name, addr = addr, ""
            result.append(Address(name=name, email=addr))
    return result


def parse_date(value: object) -> Optional[datetime]:
    """Parse a ``Date:`` header, tolerating the many broken variants."""
    if not value:
        return None
    text = decode_header_value(value)
    try:
        parsed = email.utils.parsedate_to_datetime(text)
        if parsed is not None:
            return parsed
    except Exception:
        pass
    try:
        tup = email.utils.parsedate_tz(text)
        if tup:
            stamp = email.utils.mktime_tz(tup)
            return datetime.fromtimestamp(stamp, tz=timezone.utc)
    except Exception:
        pass
    logger.debug("Unparsable Date header: %r", text[:80])
    return None


# --------------------------------------------------------------------- payload
def decode_part_payload(part: Message) -> bytes:
    """Return the decoded bytes of a leaf part, whatever its CTE.

    ``Message.get_payload(decode=True)`` already handles base64 and
    quoted-printable, but it returns ``None`` on multipart parts and silently
    returns the raw text when the base64 is corrupt.  We repair both cases.
    """
    if part is None:
        return b""
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        logger.debug("get_payload(decode=True) failed", exc_info=True)
        payload = None

    if payload is not None:
        return payload

    # Fall back to manual decoding of whatever the part holds.
    try:
        raw = part.get_payload()
    except Exception:
        return b""
    if isinstance(raw, list):  # multipart: no payload of its own
        return b""
    if raw is None:
        return b""
    data = raw.encode("utf-8", errors="surrogateescape") if isinstance(raw, str) else raw

    cte = str(part.get("Content-Transfer-Encoding", "")).strip().lower()
    try:
        if cte == "base64":
            import base64 as _b64

            return _b64.b64decode(b"".join(data.split()), validate=False)
        if cte in ("quoted-printable", "quopri"):
            return quopri.decodestring(data)
    except (binascii.Error, ValueError):
        logger.debug("Broken %s payload, using raw bytes", cte)
    return data


def decode_part_text(part: Message) -> tuple[str, str]:
    """Decode a ``text/*`` part into a string plus the charset that worked."""
    raw = decode_part_payload(part)
    declared = None
    try:
        declared = part.get_content_charset()
    except Exception:
        declared = None
    if not declared:
        try:
            declared = part.get_charset()  # type: ignore[assignment]
        except Exception:
            declared = None
    return decode_bytes(raw, str(declared) if declared else None)


def estimate_part_size(part: Message) -> int:
    """Size of a part's payload *without* decoding it.

    Exact for base64 (the common case for attachments), a close upper bound for
    quoted-printable.  Used so that listing a 20 MB mail stays cheap.
    """
    try:
        raw = part.get_payload()
    except Exception:
        return 0
    if not isinstance(raw, str):
        return 0

    cte = str(part.get("Content-Transfer-Encoding", "")).strip().lower()
    if cte == "base64":
        compact = "".join(raw.split())
        padding = compact.count("=", max(0, len(compact) - 2))
        return max(0, len(compact) // 4 * 3 - padding)
    if cte in ("quoted-printable", "quopri"):
        # Each "=XX" triplet becomes one byte; soft line breaks disappear.
        return max(0, len(raw) - 2 * raw.count("=") - raw.count("\n"))
    return len(raw.encode("utf-8", errors="surrogateescape"))


def joined_header(part: Message, name: str) -> str:
    """All occurrences of a header, decoded and joined - never ``None``."""
    try:
        values: Sequence[object] = part.get_all(name) or ()
    except Exception:
        return ""
    return ", ".join(decode_header_value(v) for v in values if v is not None)
