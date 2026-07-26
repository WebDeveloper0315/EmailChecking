"""Outgoing mail: building messages and sending them over SMTP.

The module is deliberately split in two halves:

* :func:`build_message` turns a :class:`Draft` into a correct MIME message.  It
  is pure, so the tests can build a message, parse it back with our own parser
  and assert on the result - no server involved.
* :class:`SmtpSender` does the network part and nothing else.

MIME layout produced (only the parts that are needed are created)::

    multipart/mixed            <- when there are attachments
      multipart/related        <- when there are inline (cid:) images
        multipart/alternative  <- when there is both text and HTML
          text/plain
          text/html
        image/...  (Content-ID: <...>, inline)
      application/...          (attachments)

``EmailMessage`` from the standard library builds that tree for us, which is
much safer than assembling ``MIMEMultipart`` by hand: it picks transfer
encodings, encodes non-ASCII headers as RFC 2047 and keeps the parts in the
order required by RFC 2046.
"""

from __future__ import annotations

import mimetypes
import smtplib
import socket
import ssl
from dataclasses import dataclass, field
from email.headerregistry import Address as HeaderAddress
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid, parseaddr
from pathlib import Path
from typing import Iterable, Optional, Sequence

from config import SmtpSettings
from html_processor import html_to_text, sanitize_html
from logging_setup import get_logger, register_secret
from models import Address, Attachment, Email, format_addresses

logger = get_logger("smtp")

__all__ = [
    "Draft",
    "DraftAttachment",
    "SendError",
    "SendResult",
    "SmtpSender",
    "build_message",
    "build_reply",
    "build_forward",
    "quote_original",
]


class SendError(RuntimeError):
    """SMTP failure, already phrased for a human.

    ``retryable`` marks failures that are worth queueing and trying again later
    (the server was unreachable), as opposed to failures that will never
    succeed unattended (bad password, rejected recipient).
    """

    def __init__(self, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class _ConnectFailure(Exception):
    """Internal: could not establish a session on this port/encryption pair."""

    def __init__(self, message: str, original: Exception) -> None:
        super().__init__(message)
        self.original = original


def probe_smtp_ports(host: str, ports: Sequence[int] = (587, 465, 25),
                     timeout: float = 3.0) -> dict[int, bool]:
    """Which SMTP ports of ``host`` accept a TCP connection right now.

    The probes run in parallel so a diagnosis costs one timeout, not three.
    """
    import concurrent.futures

    def probe(port: int) -> tuple[int, bool]:
        connection = socket.socket()
        connection.settimeout(timeout)
        try:
            connection.connect((host, port))
            return port, True
        except Exception:
            return port, False
        finally:
            connection.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(ports)) as pool:
        return dict(pool.map(probe, ports))


def diagnose_unreachable(host: str) -> str:
    """Explain *why* nothing could be reached, with the usual culprits.

    Called only after a send has already failed, because it costs a few
    seconds of probing.
    """
    reachable = probe_smtp_ports(host)
    open_ports = [port for port, ok in reachable.items() if ok]
    if open_ports:
        return (f"Port {open_ports[0]} of {host} does answer - retry with that port "
                f"in Settings → Sending.")

    hint = [f"No SMTP port of {host} (587, 465, 25) accepts a connection."]
    try:
        control = socket.socket()
        control.settimeout(4)
        try:
            control.connect(("example.com", 80))
            hint.append("Plain internet access works, so this is a targeted block.")
        except Exception:
            hint.append("General internet access also fails - check the connection first.")
        finally:
            control.close()
    except Exception:
        pass

    hint.append(
        "The usual causes are a VPN whose exit forbids SMTP, an ISP that blocks "
        "outgoing mail ports, or a corporate firewall. Try disconnecting the VPN "
        "(or excluding this application from it) and send again."
    )
    return " ".join(hint)


@dataclass
class DraftAttachment:
    """A file to attach; ``content_id`` makes it an inline image."""

    filename: str
    data: bytes
    content_type: str = ""
    content_id: str = ""

    @classmethod
    def from_path(cls, path: str | Path, content_id: str = "") -> "DraftAttachment":
        file_path = Path(path)
        guessed, _ = mimetypes.guess_type(file_path.name)
        return cls(
            filename=file_path.name,
            data=file_path.read_bytes(),
            content_type=guessed or "application/octet-stream",
            content_id=content_id,
        )

    @classmethod
    def from_attachment(cls, attachment: Attachment) -> "DraftAttachment":
        """Carry an attachment of a received message into a forward."""
        return cls(
            filename=attachment.filename,
            data=attachment.data,
            content_type=attachment.content_type,
            content_id=attachment.content_id,
        )

    @property
    def is_inline(self) -> bool:
        return bool(self.content_id)

    @property
    def maintype(self) -> str:
        return (self.content_type or "application/octet-stream").split("/")[0]

    @property
    def subtype(self) -> str:
        parts = (self.content_type or "application/octet-stream").split("/")
        return parts[1] if len(parts) > 1 else "octet-stream"


@dataclass
class Draft:
    """Everything the compose window collects."""

    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    subject: str = ""
    body_text: str = ""
    body_html: str = ""
    attachments: list[DraftAttachment] = field(default_factory=list)
    from_address: str = ""
    from_name: str = ""
    reply_to: str = ""
    high_priority: bool = False
    in_reply_to: str = ""
    references: list[str] = field(default_factory=list)
    message_id: str = ""

    @property
    def recipients(self) -> list[str]:
        """Envelope recipients: To + Cc + Bcc."""
        everyone: list[str] = []
        for group in (self.to, self.cc, self.bcc):
            for entry in group:
                for _, address in getaddresses([entry]):
                    if address and address not in everyone:
                        everyone.append(address)
        return everyone

    @property
    def inline_images(self) -> list[DraftAttachment]:
        return [a for a in self.attachments if a.is_inline]

    @property
    def files(self) -> list[DraftAttachment]:
        return [a for a in self.attachments if not a.is_inline]

    def validate(self) -> list[str]:
        """Problems that should stop a send, in user words."""
        problems: list[str] = []
        if not self.recipients:
            problems.append("There is no recipient.")
        if not self.from_address:
            problems.append("No sender address is configured.")
        for entry in self.to + self.cc + self.bcc:
            for _, address in getaddresses([entry]):
                if address and "@" not in address:
                    problems.append(f"{address!r} is not a valid e-mail address.")
        return problems


@dataclass
class SendResult:
    raw: bytes
    message_id: str
    recipients: list[str]
    refused: dict[str, tuple[int, bytes]] = field(default_factory=dict)


# --------------------------------------------------------------- building
def _set_address_header(message: EmailMessage, header: str, values: Sequence[str]) -> None:
    """Set an address header, keeping display names and UTF-8 intact."""
    addresses: list[HeaderAddress] = []
    for entry in values:
        for name, address in getaddresses([entry]):
            if not address:
                continue
            local, _, domain = address.partition("@")
            try:
                addresses.append(HeaderAddress(display_name=name, username=local, domain=domain))
            except (ValueError, IndexError):
                logger.debug("Skipping unparsable address %r", address)
    if addresses:
        message[header] = addresses


def build_message(draft: Draft, include_bcc: bool = False) -> EmailMessage:
    """Turn a draft into a ready-to-send :class:`EmailMessage`.

    ``include_bcc`` adds the ``Bcc`` header, which must only happen for the copy
    stored in the Sent folder - never for the copy handed to the SMTP server,
    where it would disclose the blind recipients.
    """
    message = EmailMessage()

    sender = draft.from_address
    if draft.from_name:
        name, address = draft.from_name, sender
        local, _, domain = address.partition("@")
        try:
            message["From"] = HeaderAddress(display_name=name, username=local, domain=domain)
        except (ValueError, IndexError):
            message["From"] = sender
    else:
        message["From"] = sender

    _set_address_header(message, "To", draft.to)
    if draft.cc:
        _set_address_header(message, "Cc", draft.cc)
    if include_bcc and draft.bcc:
        _set_address_header(message, "Bcc", draft.bcc)
    if draft.reply_to:
        _set_address_header(message, "Reply-To", [draft.reply_to])

    message["Subject"] = draft.subject or ""
    message["Date"] = formatdate(localtime=True)
    domain = sender.partition("@")[2] or None
    message["Message-ID"] = draft.message_id or make_msgid(domain=domain)

    # Threading (RFC 5322 §3.6.4): a reply must point at its parent and carry
    # the whole ancestry, otherwise clients start a new thread.
    if draft.in_reply_to:
        message["In-Reply-To"] = draft.in_reply_to
    if draft.references:
        message["References"] = " ".join(draft.references)

    if draft.high_priority:
        message["X-Priority"] = "1 (Highest)"
        message["Importance"] = "High"
        message["X-MSMail-Priority"] = "High"

    message["X-Mailer"] = "Mail Viewer"

    text = draft.body_text or (html_to_text(draft.body_html) if draft.body_html else "")
    message.set_content(text or "", subtype="plain", charset="utf-8")

    if draft.body_html:
        message.add_alternative(draft.body_html, subtype="html", charset="utf-8")
        for image in draft.inline_images:
            # add_related() attaches to the HTML part, producing multipart/related.
            html_part = message.get_payload()[-1]
            html_part.add_related(
                image.data,
                maintype=image.maintype,
                subtype=image.subtype,
                cid=f"<{image.content_id}>",
                filename=image.filename,
                disposition="inline",
            )

    for attachment in draft.files:
        if attachment.maintype == "message":
            # RFC 2046 §5.2.1: a message/rfc822 body must stay 7bit/8bit/binary.
            # Handing raw bytes to add_attachment() would base64-encode it,
            # which several clients (including our own parser's strict path)
            # then fail to open.  Attaching a parsed message object makes the
            # stdlib emit the correct 8bit part instead.
            message.add_attachment(_as_message(attachment.data),
                                   filename=attachment.filename)
            continue
        message.add_attachment(
            attachment.data,
            maintype=attachment.maintype,
            subtype=attachment.subtype,
            filename=attachment.filename,
        )

    return message


def _as_message(raw: bytes) -> EmailMessage:
    """Parse raw bytes into an EmailMessage for use as a message/rfc822 part."""
    import email as email_module
    from email import policy

    parsed = email_module.message_from_bytes(raw, policy=policy.default)
    if not isinstance(parsed, EmailMessage):  # pragma: no cover - policy guarantees it
        wrapper = EmailMessage()
        wrapper.set_content(raw.decode("utf-8", "replace"))
        return wrapper
    return parsed


def draft_to_bytes(draft: Draft, include_bcc: bool = False) -> bytes:
    return build_message(draft, include_bcc=include_bcc).as_bytes()


# ---------------------------------------------------------------- quoting
def _attribution(original: Email) -> str:
    when = original.display_date or original.date_raw or "an earlier date"
    who = original.sender or "someone"
    return f"On {when}, {who} wrote:"


def quote_original(original: Email) -> tuple[str, str]:
    """Return ``(quoted_text, quoted_html)`` for a reply."""
    attribution = _attribution(original)
    source_text = original.body_text()
    quoted_text = attribution + "\n" + "\n".join(
        f"> {line}" for line in source_text.splitlines()
    )

    if original.has_html:
        # The original HTML is untrusted: sanitise before putting it into our
        # own editor, or a reply would re-introduce scripts and trackers.
        safe = sanitize_html(original.html_body, allow_remote_images=False).html
    else:
        from html_processor import text_to_html

        safe = text_to_html(source_text)
    quoted_html = (
        f"<p></p><p>{_escape(attribution)}</p>"
        '<blockquote style="margin:0 0 0 12px;padding-left:12px;'
        'border-left:2px solid #cccccc;color:#555555">'
        f"{safe}</blockquote>"
    )
    return quoted_text, quoted_html


def _escape(text: str) -> str:
    import html as html_module

    return html_module.escape(text, quote=False)


def _addresses_of(entries: Iterable[Address]) -> list[str]:
    return [str(a) for a in entries if a.email]


def build_reply(
    original: Email,
    from_address: str,
    from_name: str = "",
    reply_all: bool = False,
) -> Draft:
    """Reply / Reply all, with threading headers and the original quoted."""
    targets = _addresses_of(original.reply_to) or _addresses_of(original.from_addrs)

    cc: list[str] = []
    if reply_all:
        own = {from_address.lower()}
        own |= {a.email.lower() for a in original.reply_to if a.email}
        extra = [
            str(a) for a in list(original.to_addrs) + list(original.cc_addrs)
            if a.email and a.email.lower() not in own
            and a.email.lower() not in {parseaddr(t)[1].lower() for t in targets}
        ]
        seen: set[str] = set()
        for entry in extra:
            key = parseaddr(entry)[1].lower()
            if key and key not in seen:
                seen.add(key)
                cc.append(entry)

    subject = original.subject or ""
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}" if subject else "Re:"

    quoted_text, quoted_html = quote_original(original)
    references = list(original.references)
    if original.message_id and original.message_id not in references:
        references.append(original.message_id)

    return Draft(
        to=targets,
        cc=cc,
        subject=subject,
        body_text="\n\n" + quoted_text,
        body_html="<p></p>" + quoted_html,
        from_address=from_address,
        from_name=from_name,
        in_reply_to=original.message_id,
        references=references,
    )


def build_forward(
    original: Email,
    from_address: str,
    from_name: str = "",
    as_attachment: bool = False,
    raw_message: Optional[bytes] = None,
) -> Draft:
    """Forward inline, or as a ``message/rfc822`` attachment.

    ``raw_message`` is the untouched source of the original mail; it is only
    needed for ``as_attachment`` and comes from the message cache.
    """
    subject = original.subject or ""
    if not subject.lower().startswith(("fwd:", "fw:")):
        subject = f"Fwd: {subject}" if subject else "Fwd:"

    draft = Draft(
        to=[],
        subject=subject,
        from_address=from_address,
        from_name=from_name,
    )

    if as_attachment:
        if raw_message is None:
            raw_message = build_message(
                Draft(subject=original.subject, body_text=original.body_text(),
                      from_address=original.from_addrs[0].email if original.from_addrs else "",
                      to=[str(a) for a in original.to_addrs])
            ).as_bytes()
        name = (original.subject or "forwarded message").strip()[:60] or "message"
        draft.attachments.append(
            DraftAttachment(filename=f"{name}.eml", data=raw_message,
                            content_type="message/rfc822")
        )
        draft.body_text = "\n\nSee the attached message.\n"
        draft.body_html = "<p></p><p>See the attached message.</p>"
        return draft

    header_lines = [
        ("From", original.sender),
        ("Date", original.display_date or original.date_raw),
        ("Subject", original.subject),
        ("To", format_addresses(original.to_addrs)),
    ]
    if original.cc_addrs:
        header_lines.append(("Cc", format_addresses(original.cc_addrs)))

    text_header = "\n".join(f"{name}: {value}" for name, value in header_lines if value)
    draft.body_text = (
        "\n\n---------- Forwarded message ----------\n"
        f"{text_header}\n\n{original.body_text()}"
    )

    if original.has_html:
        safe = sanitize_html(original.html_body, allow_remote_images=False).html
    else:
        from html_processor import text_to_html

        safe = text_to_html(original.body_text())
    html_header = "<br>".join(
        f"<b>{_escape(name)}:</b> {_escape(value)}" for name, value in header_lines if value
    )
    draft.body_html = (
        "<p></p><p>---------- Forwarded message ----------</p>"
        f"<p>{html_header}</p><div>{safe}</div>"
    )

    # Inline images and files travel with an inline forward.
    for attachment in list(original.attachments) + list(original.inline_images):
        try:
            draft.attachments.append(DraftAttachment.from_attachment(attachment))
        except Exception:
            logger.warning("Could not carry attachment %s into the forward",
                           attachment.filename, exc_info=True)
    return draft


# ---------------------------------------------------------------- sending
class SmtpSender:
    """One-shot SMTP connection.  Create, :meth:`send`, discard."""

    def __init__(self, settings: SmtpSettings) -> None:
        self.settings = settings
        register_secret(settings.password)

    def send(self, draft: Draft) -> SendResult:
        problems = draft.validate()
        if problems:
            raise SendError("\n".join(problems))

        message = build_message(draft, include_bcc=False)
        return self.send_raw(
            message.as_bytes(),
            draft.from_address,
            draft.recipients,
            message_id=str(message.get("Message-ID", "")),
        )

    def send_raw(self, raw: bytes, sender: str, recipients: Sequence[str],
                 message_id: str = "") -> SendResult:
        """Send an already built message - also used to retry the outbox."""
        recipients = list(recipients)
        if not recipients:
            raise SendError("There is no recipient.")
        logger.info("Sending message to %d recipient(s)", len(recipients),
                    extra={"event": "send", "recipients": len(recipients),
                           "bytes": len(raw), **self.settings.redacted()})

        # Try the configured port first, then the other standard pairs: a
        # network that blocks 587 often still allows 465.
        attempts: list[tuple[int, str]] = [(self.settings.port, self.settings.security)]
        if self.settings.auto_port_fallback:
            attempts.extend(self.settings.alternatives())

        failures: list[str] = []
        for port, security in attempts:
            try:
                return self._send_via(raw, sender, recipients, message_id, port, security)
            except _ConnectFailure as exc:
                failures.append(f"port {port} ({security}): {exc}")
                logger.info("SMTP port %s unusable, trying the next one", port,
                            extra={"event": "smtp_fallback", "port": port,
                                   "security": security})

        detail = "\n".join(f"  • {line}" for line in failures)
        raise SendError(
            f"Cannot reach {self.settings.host}.\n{detail}\n\n"
            f"{diagnose_unreachable(self.settings.host)}",
            retryable=True,
        )

    def _send_via(self, raw: bytes, sender: str, recipients: list[str],
                  message_id: str, port: int, security: str) -> SendResult:
        """One full attempt on a specific port/encryption pair."""
        server: Optional[smtplib.SMTP] = None
        try:
            server = self._connect(port, security)
            server.ehlo()
            if security == "starttls":
                context = ssl.create_default_context()
                server.starttls(context=context)
                server.ehlo()
            if self.settings.username:
                server.login(self.settings.username, self.settings.password)
            refused = server.sendmail(sender, recipients, raw)
        except smtplib.SMTPAuthenticationError as exc:
            raise SendError(self._auth_hint(exc)) from exc
        except smtplib.SMTPRecipientsRefused as exc:
            details = ", ".join(f"{addr} ({code})" for addr, (code, _) in exc.recipients.items())
            raise SendError(f"The server rejected these recipients: {details}") from exc
        except smtplib.SMTPSenderRefused as exc:
            raise SendError(
                f"The server refused {sender} as sender: {exc.smtp_error!r}.\n"
                "Usually the From address must match the account you log in with."
            ) from exc
        except smtplib.SMTPDataError as exc:
            raise SendError(f"The server rejected the message: {exc.smtp_error!r}") from exc
        except smtplib.SMTPServerDisconnected as exc:
            raise SendError(f"The connection to {self.settings.host} dropped: {exc}",
                            retryable=True) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise _ConnectFailure(
                f"no answer within {self.settings.timeout} s", exc
            ) from exc
        except ssl.SSLError as exc:
            raise _ConnectFailure(f"TLS handshake failed ({exc})", exc) from exc
        except socket.gaierror as exc:
            raise SendError(f"Cannot find the SMTP server {self.settings.host}.",
                            retryable=True) from exc
        except (ConnectionRefusedError, OSError) as exc:
            raise _ConnectFailure(str(exc), exc) from exc
        finally:
            if server is not None:
                try:
                    server.quit()
                except Exception:
                    pass

        logger.info("Message sent", extra={"event": "sent", "message_id": message_id,
                                           "refused": len(refused)})
        return SendResult(raw=raw, message_id=message_id, recipients=recipients, refused=refused)

    def _connect(self, port: int, security: str) -> smtplib.SMTP:
        host, timeout = self.settings.host, self.settings.timeout
        if not host:
            raise SendError("No SMTP server is configured (Settings → Sending).")
        if security == "ssl":
            context = ssl.create_default_context()
            return smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
        return smtplib.SMTP(host, port, timeout=timeout)

    def _auth_hint(self, exc: smtplib.SMTPAuthenticationError) -> str:
        host = self.settings.host.lower()
        hint = ""
        if "gmail" in host:
            hint = ("\n\nGmail requires a 16-character app password "
                    "(https://myaccount.google.com/apppasswords).")
        elif "office365" in host or "outlook" in host:
            hint = ("\n\nMicrosoft 365 often disables basic authentication; "
                    "an app password or OAuth is required.")
        return (f"SMTP login failed for {self.settings.username}: "
                f"{exc.smtp_code} {exc.smtp_error!r}{hint}")
