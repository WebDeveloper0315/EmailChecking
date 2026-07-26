"""A fake ``smtplib`` server, so sending can be tested without a real one."""

from __future__ import annotations

import smtplib
from contextlib import contextmanager
from typing import Iterator, Optional

__all__ = ["FakeSMTP", "fake_smtp_server"]


class FakeSMTP:
    """Records what a client does and can be told to fail like a real server."""

    instances: list["FakeSMTP"] = []

    #: Set by :func:`fake_smtp_server` to script failures.
    fail_on: Optional[str] = None
    accept_password: str = "secret"
    refuse_recipients: tuple[str, ...] = ()

    def __init__(self, host: str = "", port: int = 0, timeout: float = 30, context=None,
                 **kwargs) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.context = context
        self.calls: list[tuple] = []
        self.sent: list[tuple[str, list[str], bytes]] = []
        self.logged_in_as: Optional[str] = None
        self.started_tls = False
        self.quit_called = False
        type(self).instances.append(self)

    # ------------------------------------------------------------ protocol
    def ehlo(self, name: str = ""):
        self.calls.append(("ehlo", name))
        if type(self).fail_on == "ehlo":
            raise smtplib.SMTPServerDisconnected("connection dropped during EHLO")
        return 250, b"OK"

    def starttls(self, context=None, **kwargs):
        self.calls.append(("starttls",))
        if type(self).fail_on == "starttls":
            import ssl

            raise ssl.SSLError("certificate verify failed")
        self.started_tls = True
        return 220, b"Ready to start TLS"

    def login(self, username: str, password: str):
        self.calls.append(("login", username))
        if type(self).fail_on == "login" or password != type(self).accept_password:
            raise smtplib.SMTPAuthenticationError(
                535, b"5.7.8 Username and Password not accepted"
            )
        self.logged_in_as = username
        return 235, b"Accepted"

    def sendmail(self, from_addr: str, to_addrs, msg, *args, **kwargs):
        recipients = list(to_addrs)
        self.calls.append(("sendmail", from_addr, tuple(recipients)))
        if type(self).fail_on == "sendmail":
            raise smtplib.SMTPDataError(554, b"5.7.1 Message rejected")
        if type(self).fail_on == "sender":
            raise smtplib.SMTPSenderRefused(553, b"5.7.1 Sender denied", from_addr)
        raw = msg if isinstance(msg, bytes) else str(msg).encode()
        refused = {
            address: (550, b"5.1.1 No such user")
            for address in recipients if address in type(self).refuse_recipients
        }
        if refused and len(refused) == len(recipients):
            raise smtplib.SMTPRecipientsRefused(refused)
        self.sent.append((from_addr, recipients, raw))
        return refused

    def quit(self):
        self.calls.append(("quit",))
        self.quit_called = True
        return 221, b"Bye"


@contextmanager
def fake_smtp_server(fail_on: Optional[str] = None, accept_password: str = "secret",
                     refuse_recipients: tuple[str, ...] = ()) -> Iterator[type[FakeSMTP]]:
    """Replace ``smtplib.SMTP`` / ``SMTP_SSL`` with the fake for the block."""
    original_smtp = smtplib.SMTP
    original_ssl = smtplib.SMTP_SSL
    FakeSMTP.instances = []
    FakeSMTP.fail_on = fail_on
    FakeSMTP.accept_password = accept_password
    FakeSMTP.refuse_recipients = refuse_recipients

    import mail_sender

    # The failure diagnosis probes real sockets; stub it so tests stay offline.
    original_diagnose = mail_sender.diagnose_unreachable
    mail_sender.diagnose_unreachable = lambda host: "(diagnosis skipped in tests)"

    mail_sender.smtplib.SMTP = FakeSMTP          # type: ignore[assignment]
    mail_sender.smtplib.SMTP_SSL = FakeSMTP      # type: ignore[assignment]
    try:
        yield FakeSMTP
    finally:
        mail_sender.smtplib.SMTP = original_smtp      # type: ignore[assignment]
        mail_sender.smtplib.SMTP_SSL = original_ssl   # type: ignore[assignment]
        mail_sender.diagnose_unreachable = original_diagnose
        FakeSMTP.fail_on = None
        FakeSMTP.refuse_recipients = ()
