"""Structured logging for the whole application.

Two sinks:

* the console, in a compact human-readable format;
* ``logs/mailviewer.log``, one JSON object per line (rotating), which is what
  "structured" buys you: every record carries ``component``, ``event`` and any
  extra keys, so a sync problem can be grepped as
  ``jq 'select(.component=="imap")' logs/mailviewer.log``.

Secrets never reach either sink.  :class:`SecretRedactingFilter` is installed on
the root logger and rewrites both the message and its arguments; passwords are
registered with :func:`register_secret` as soon as they are known (login,
settings dialog, config load), and common ``password=...`` shapes are scrubbed
even when the value was never registered.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "configure",
    "get_logger",
    "register_secret",
    "SecretRedactingFilter",
    "REDACTED",
]

REDACTED = "***"

#: Components used across the app - keep them stable, they are grep keys.
COMPONENTS = ("imap", "smtp", "sync", "send", "delete", "parse", "ui", "config")

_LOG_DIR = Path(__file__).resolve().with_name("logs")
_CONSOLE_FORMAT = "%(asctime)s %(levelname)-7s %(name)-18s %(message)s"

#: ``password = "hunter2"``, ``password: hunter2``, ``pass=hunter2`` ...
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(password|passwd|pwd|secret|token|app[_-]?password)\b\s*[=:]\s*"
               r"(['\"]?)([^\s'\",;)]+)\2"),
    re.compile(r"(?i)\b(LOGIN|AUTHENTICATE)\s+(\S+)\s+(\S+)"),
)


def _redact_patterns(text: str) -> str:
    text = _SECRET_PATTERNS[0].sub(lambda m: f"{m.group(1)}={REDACTED}", text)
    return _SECRET_PATTERNS[1].sub(lambda m: f"{m.group(1)} {m.group(2)} {REDACTED}", text)


class SecretRedactingFilter(logging.Filter):
    """Removes registered secrets and password-shaped text from every record."""

    _secrets: set[str] = set()

    @classmethod
    def register(cls, secret: str | None) -> None:
        if secret and len(secret) >= 4:
            cls._secrets.add(secret)

    @classmethod
    def scrub(cls, value: Any) -> Any:
        if isinstance(value, str):
            for secret in cls._secrets:
                if secret in value:
                    value = value.replace(secret, REDACTED)
            return _redact_patterns(value)
        if isinstance(value, bytes):
            try:
                return cls.scrub(value.decode("utf-8", "replace")).encode("utf-8")
            except Exception:
                return value
        if isinstance(value, dict):
            return {k: (REDACTED if _is_secret_key(k) else cls.scrub(v)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(cls.scrub(v) for v in value)
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = self.scrub(record.msg)
            if record.args:
                record.args = self.scrub(record.args)
            for key, value in list(getattr(record, "__dict__", {}).items()):
                if key in _RESERVED or not isinstance(value, (str, bytes, dict, list, tuple)):
                    continue
                record.__dict__[key] = REDACTED if _is_secret_key(key) else self.scrub(value)
        except Exception:  # logging must never raise
            pass
        return True


def _is_secret_key(key: str) -> bool:
    lowered = str(key).lower()
    return any(token in lowered for token in ("password", "passwd", "secret", "token", "pwd"))


#: LogRecord attributes that must not be touched by the filter.
_RESERVED = frozenset(
    """args asctime created exc_info exc_text filename funcName levelname levelno
    lineno module msecs message msg name pathname process processName relativeCreated
    stack_info thread threadName taskName""".split()
)


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with any ``extra=`` keys included."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "component": getattr(record, "component", record.name.split(".")[-1]),
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key in payload or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        try:
            return json.dumps(payload, ensure_ascii=False)
        except Exception:
            return json.dumps({"level": record.levelname, "message": str(record.msg)})


def configure(
    level: str = "INFO",
    log_dir: Path | str | None = None,
    console: bool = True,
    extra_secrets: Iterable[str] = (),
) -> Path | None:
    """Set up console + rotating JSON file logging.  Returns the log file path."""
    for secret in extra_secrets:
        SecretRedactingFilter.register(secret)

    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    redactor = SecretRedactingFilter()

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter(_CONSOLE_FORMAT, datefmt="%H:%M:%S"))
        stream.addFilter(redactor)
        root.addHandler(stream)

    log_path: Path | None = None
    directory = Path(log_dir) if log_dir else _LOG_DIR
    try:
        directory.mkdir(parents=True, exist_ok=True)
        log_path = directory / "mailviewer.log"
        rotating = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        rotating.setFormatter(JsonFormatter())
        rotating.addFilter(redactor)
        root.addHandler(rotating)
    except OSError:
        log_path = None
        logging.getLogger(__name__).warning("Cannot write log files to %s", directory)

    # Third-party noise: imbox logs one line per fetched message at INFO.
    logging.getLogger("imbox").setLevel(logging.WARNING)
    logging.getLogger("chardet").setLevel(logging.WARNING)
    # Belt and braces: these can be switched to DEBUG by hand and would then
    # print protocol traffic, including the LOGIN command.
    logging.getLogger("imaplib").setLevel(logging.INFO)
    logging.getLogger("smtplib").setLevel(logging.INFO)
    return log_path


def register_secret(secret: str | None) -> None:
    """Tell the logger about a password so it is masked everywhere."""
    SecretRedactingFilter.register(secret)


class _ComponentAdapter(logging.LoggerAdapter):
    """Adds ``component`` while keeping the caller's ``extra=`` fields.

    The default ``LoggerAdapter.process`` *replaces* ``kwargs["extra"]`` with the
    adapter's own dictionary, which would silently drop every structured field
    passed at the call site.  Merging is the whole point here.
    """

    def process(self, msg, kwargs):  # noqa: ANN001 - stdlib signature
        extra = dict(self.extra or {})
        extra.update(kwargs.get("extra") or {})
        kwargs["extra"] = extra
        return msg, kwargs


def get_logger(component: str, name: str | None = None) -> logging.LoggerAdapter:
    """Logger that stamps every record with a component, for the JSON sink."""
    logger = logging.getLogger(name or f"mail.{component}")
    return _ComponentAdapter(logger, {"component": component})
