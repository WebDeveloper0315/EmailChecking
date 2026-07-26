"""Turn raw RFC 5322 bytes into an :class:`~models.Email`.

Design notes
------------
* ``compat32`` policy on purpose.  ``email.policy.default`` pre-decodes headers
  but raises / produces ``Header`` defect objects on the malformed input that
  real mailboxes are full of.  With ``compat32`` we get the raw strings and do
  the decoding ourselves in :mod:`mime_decoder`, where every step has a
  fallback.
* Body selection follows RFC 2046: inside ``multipart/alternative`` only the
  *best* representation is kept (the last part the sender listed, which is the
  richest one), everything else is concatenated in document order.
* Attachment payloads are never decoded here.  Each :class:`~models.Attachment`
  keeps a closure over its MIME part and decodes on first access, so listing a
  20 MB message costs nothing.
"""

from __future__ import annotations

import logging
import mimetypes
from dataclasses import dataclass, field
from email.message import Message
from email.parser import BytesParser
from email.policy import compat32
from typing import Optional

from mime_decoder import (
    decode_addresses,
    decode_bytes,
    decode_header_value,
    decode_part_payload,
    decode_part_text,
    estimate_part_size,
    normalize_charset,
    parse_date,
)
from models import Address, Attachment, Email

logger = logging.getLogger(__name__)

__all__ = ["parse_email", "MailParser"]

#: Guard against hand-crafted or corrupt messages with absurd nesting.
MAX_DEPTH = 30
#: Guard against multipart bombs.
MAX_PARTS = 5000

_HEADER_ORDER = ("From", "To", "Cc", "Bcc", "Subject", "Date", "Reply-To", "Message-ID")


@dataclass
class _Collector:
    """Mutable state carried through the recursive walk."""

    text_parts: list[str] = field(default_factory=list)
    html_parts: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    inline_images: list[Attachment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    part_count: int = 0

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)


class MailParser:
    """Stateless parser; kept as a class so it can be configured/injected."""

    def parse(
        self,
        raw: bytes | str,
        uid: str = "",
        source: str = "",
        folder: str = "",
        flags: frozenset[str] = frozenset(),
    ) -> Email:
        """Parse raw message bytes.  Always returns an :class:`Email`."""
        data = self._as_bytes(raw)
        mail = Email(uid=uid, raw_size=len(data), source=source,
                     folder=folder or source, flags=frozenset(flags))

        try:
            message = BytesParser(policy=compat32).parsebytes(data)
        except Exception as exc:
            logger.exception("Unparsable message uid=%s", uid)
            mail.warnings.append(f"Message could not be parsed: {exc}")
            text, _ = decode_bytes(data)
            mail.text_body = text
            mail.subject = "(unparsable message)"
            return mail

        self._read_headers(message, mail)

        collector = _Collector()
        try:
            self._walk(message, collector, depth=0)
        except Exception as exc:
            logger.exception("MIME walk failed for uid=%s", uid)
            collector.warn(f"MIME structure is broken, output may be incomplete: {exc}")

        mail.text_body = "\n\n".join(p for p in collector.text_parts if p.strip())
        mail.html_body = self._join_html(collector.html_parts)
        mail.attachments = collector.attachments
        mail.inline_images = collector.inline_images
        mail.warnings.extend(collector.warnings)

        for defect in getattr(message, "defects", []) or []:
            collector.warn(f"MIME defect: {type(defect).__name__}")

        if not mail.text_body and not mail.html_body and not mail.attachments:
            mail.warnings.append("No readable body found in this message.")
        return mail

    # ---------------------------------------------------------------- headers
    @staticmethod
    def _as_bytes(raw: bytes | str) -> bytes:
        if isinstance(raw, bytes):
            return raw
        if isinstance(raw, bytearray):
            return bytes(raw)
        return str(raw).encode("utf-8", errors="surrogateescape")

    def _read_headers(self, message: Message, mail: Email) -> None:
        get = message.get

        mail.subject = decode_header_value(get("Subject"))
        mail.from_addrs = decode_addresses(message.get_all("From"))
        mail.to_addrs = decode_addresses(message.get_all("To"))
        mail.cc_addrs = decode_addresses(message.get_all("Cc"))
        mail.bcc_addrs = decode_addresses(message.get_all("Bcc"))
        mail.reply_to = decode_addresses(message.get_all("Reply-To"))
        mail.date_raw = decode_header_value(get("Date"))
        mail.date = parse_date(get("Date"))
        mail.message_id = decode_header_value(get("Message-ID")).strip()
        mail.in_reply_to = decode_header_value(get("In-Reply-To")).strip()
        mail.references = [
            token for token in decode_header_value(get("References")).split() if token.strip()
        ]

        if mail.date is None and mail.date_raw:
            mail.warnings.append(f"Unreadable Date header: {mail.date_raw!r}")

        # Full header list, well-known ones first, for the "details" pane.
        seen: list[tuple[str, str]] = []
        try:
            items = list(message.items())
        except Exception:
            items = []
        for wanted in _HEADER_ORDER:
            for name, value in items:
                if name.lower() == wanted.lower():
                    seen.append((name, decode_header_value(value)))
        for name, value in items:
            if not any(name.lower() == w.lower() for w in _HEADER_ORDER):
                seen.append((name, decode_header_value(value)))
        mail.headers = seen

    # ------------------------------------------------------------------- body
    def _walk(self, part: Message, collector: _Collector, depth: int) -> None:
        collector.part_count += 1
        if depth > MAX_DEPTH or collector.part_count > MAX_PARTS:
            collector.warn("MIME tree too deep or too large; truncated.")
            return

        try:
            content_type = (part.get_content_type() or "").lower()
        except Exception:
            content_type = "text/plain"

        if content_type == "message/rfc822" or content_type.startswith("message/"):
            self._add_attached_message(part, collector)
            return

        if part.is_multipart():
            subtype = content_type.partition("/")[2]
            children = part.get_payload()
            if not isinstance(children, list):
                collector.warn("Multipart part without sub-parts (broken boundary).")
                return
            if subtype == "alternative":
                self._walk_alternative(children, collector, depth)
            else:
                for child in children:
                    if isinstance(child, Message):
                        self._walk(child, collector, depth + 1)
            return

        if content_type.startswith("multipart/"):
            # Declared multipart, but the boundary never appeared, so Python
            # left the payload as one string.  Show it instead of offering the
            # whole body as a nameless "attachment".
            collector.warn("Broken multipart message (boundary not found); "
                           "showing the raw content.")
            try:
                text, _ = decode_part_text(part)
            except Exception:
                text = ""
            if text.strip():
                collector.text_parts.append(text.replace("\r\n", "\n").replace("\r", "\n"))
            return

        self._handle_leaf(part, content_type, collector)

    def _walk_alternative(self, children: list, collector: _Collector, depth: int) -> None:
        """RFC 2046: keep the richest representation, not all of them."""
        best_html: list[str] = []
        best_text: list[str] = []
        for child in children:
            if not isinstance(child, Message):
                continue
            branch = _Collector()
            self._walk(child, branch, depth + 1)
            # Later parts are richer, so they replace earlier ones.
            if branch.html_parts:
                best_html = branch.html_parts
            if branch.text_parts:
                best_text = branch.text_parts
            collector.attachments.extend(branch.attachments)
            collector.inline_images.extend(branch.inline_images)
            for warning in branch.warnings:
                collector.warn(warning)
            collector.part_count += branch.part_count
        collector.html_parts.extend(best_html)
        collector.text_parts.extend(best_text)

    def _handle_leaf(self, part: Message, content_type: str, collector: _Collector) -> None:
        disposition = self._disposition(part)
        filename = decode_header_value(part.get_filename())
        content_id = str(part.get("Content-ID", "") or "").strip().strip("<>")
        is_text = content_type in ("text/plain", "text/html")

        looks_like_file = bool(filename) or disposition == "attachment"
        if is_text and not looks_like_file:
            try:
                text, charset = decode_part_text(part)
            except Exception as exc:  # never let one bad part kill the message
                logger.debug("Failed to decode text part", exc_info=True)
                collector.warn(f"A text part could not be decoded: {exc}")
                return
            declared = (part.get_content_charset() or "").lower()
            # Only complain when the declared charset was genuinely unusable -
            # mapping gb2312 to gb18030 or ks_c_5601-1987 to cp949 is expected.
            if declared and charset and normalize_charset(declared) != charset:
                collector.warn(f"Charset {declared!r} was wrong, decoded as {charset!r}.")
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            if content_type == "text/html":
                collector.html_parts.append(text)
            else:
                collector.text_parts.append(text)
            return

        attachment = self._build_attachment(part, content_type, filename, content_id, disposition)
        if attachment.content_id and (attachment.is_inline or attachment.is_image):
            collector.inline_images.append(attachment)
            if disposition == "attachment":
                collector.attachments.append(attachment)
        else:
            collector.attachments.append(attachment)

    def _add_attached_message(self, part: Message, collector: _Collector) -> None:
        """A forwarded mail is shown as a saveable ``.eml``, not merged inline."""
        subject = ""
        payload = part.get_payload()
        inner = payload[0] if isinstance(payload, list) and payload else None
        if isinstance(inner, Message):
            subject = decode_header_value(inner.get("Subject"))
        name = decode_header_value(part.get_filename()) or f"{subject or 'attached message'}.eml"

        def load() -> bytes:
            data = inner.as_bytes() if isinstance(inner, Message) else decode_part_payload(part)
            # A message/rfc822 part should be 7bit/8bit (RFC 2046 §5.2.1), but
            # some clients base64 it anyway.  Python's parser then hands us a
            # sub-message whose "body" is the encoded text, so decode it here
            # rather than saving an unreadable .eml.
            encoding = str(part.get("Content-Transfer-Encoding", "")).strip().lower()
            if encoding == "base64":
                import base64 as _base64
                import binascii

                try:
                    return _base64.b64decode(b"".join(data.split()), validate=False)
                except (binascii.Error, ValueError):
                    return data
            if encoding in ("quoted-printable", "quopri"):
                import quopri as _quopri

                try:
                    return _quopri.decodestring(data)
                except Exception:
                    return data
            return data

        size = 0
        try:
            size = len(inner.as_bytes()) if isinstance(inner, Message) else estimate_part_size(part)
        except Exception:
            pass
        collector.attachments.append(
            Attachment(
                filename=_safe_display_name(name, "message/rfc822"),
                content_type="message/rfc822",
                size=size,
                loader=load,
            )
        )

    def _build_attachment(
        self,
        part: Message,
        content_type: str,
        filename: str,
        content_id: str,
        disposition: str,
    ) -> Attachment:
        charset = ""
        try:
            charset = part.get_content_charset() or ""
        except Exception:
            pass
        try:
            size = estimate_part_size(part)
        except Exception:
            size = 0
        return Attachment(
            filename=_safe_display_name(filename, content_type, content_id),
            content_type=content_type or "application/octet-stream",
            size=size,
            content_id=content_id,
            is_inline=disposition == "inline" or bool(content_id),
            charset=charset,
            loader=lambda p=part: decode_part_payload(p),
        )

    @staticmethod
    def _disposition(part: Message) -> str:
        try:
            value = part.get_content_disposition()
        except Exception:
            value = None
        if value:
            return value.lower()
        raw = str(part.get("Content-Disposition", "") or "").strip().lower()
        return raw.split(";")[0].strip()

    @staticmethod
    def _join_html(html_parts: list[str]) -> str:
        parts = [p for p in html_parts if p.strip()]
        if not parts:
            return ""
        if len(parts) == 1:
            return parts[0]
        # Several HTML parts in one mixed message: stack them with a separator.
        return '<hr style="border:none;border-top:1px solid #dadce0;margin:16px 0">'.join(parts)


_UNNAMED = 0


def _safe_display_name(filename: str, content_type: str, content_id: str = "") -> str:
    """Every attachment needs a name; invent a sensible one when missing."""
    name = (filename or "").strip().strip('"')
    if name:
        return name
    if content_id:
        base = content_id.split("@")[0] or "inline"
    else:
        base = "attachment"
    extension = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ".bin"
    if extension == ".jpe":  # mimetypes' unhelpful default for image/jpeg
        extension = ".jpg"
    return f"{base}{extension}"


_DEFAULT_PARSER = MailParser()


def parse_email(
    raw: bytes | str,
    uid: str = "",
    source: str = "",
    folder: str = "",
    flags: frozenset[str] = frozenset(),
) -> Email:
    """Module level convenience wrapper around :class:`MailParser`."""
    return _DEFAULT_PARSER.parse(raw, uid=uid, source=source, folder=folder, flags=flags)


def build_cid_index(mail: Email) -> dict[str, Attachment]:
    """Map ``Content-ID`` **and** file name to the inline parts of a message.

    Some senders reference inline images by file name instead of by CID, so both
    keys are indexed.
    """
    index: dict[str, Attachment] = {}
    for attachment in list(mail.inline_images) + list(mail.attachments):
        if attachment.content_id:
            index.setdefault(attachment.content_id, attachment)
        if attachment.filename:
            index.setdefault(attachment.filename, attachment)
    return index


def find_inline_attachment(mail: Email, cid: str) -> Optional[Attachment]:
    """Resolve one ``cid:`` reference from a message body."""
    key = (cid or "").strip().strip("<>")
    if not key:
        return None
    index = build_cid_index(mail)
    return index.get(key) or index.get(key.split("@")[0])


def address_summary(addresses: list[Address], limit: int = 3) -> str:
    """``a, b and 4 more`` - used by the message list."""
    shown = [a.short for a in addresses[:limit] if a.short]
    extra = len(addresses) - len(shown)
    text = ", ".join(shown)
    return f"{text} and {extra} more" if extra > 0 else text
