"""Application settings: IMAP + SMTP accounts, sync, viewer and window state.

Settings come from three places, later ones winning:

1. the defaults below (Gmail, as in the original notebook),
2. ``config.ini`` next to this file,
3. environment variables (``MAIL_USERNAME``, ``MAIL_PASSWORD``, ``IMAP_SERVER``,
   ``IMAP_PORT``, ``SMTP_SERVER``, ``SMTP_PORT``, ``MAIL_DOWNLOAD_DIR``).

Passwords are only written to ``config.ini`` when the user explicitly asks for
it; the environment (or the login dialog) is the recommended route.
"""

from __future__ import annotations

import configparser
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

__all__ = [
    "AccountSettings",
    "SmtpSettings",
    "SyncSettings",
    "WindowState",
    "AppSettings",
    "default_config_path",
    "load_settings",
    "SYNC_INTERVALS",
]

DEFAULT_IMAP_SERVER = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993

#: (seconds, label) - 0 means "manual only".  Offered in the settings dialog.
SYNC_INTERVALS: tuple[tuple[int, str], ...] = (
    (30, "Every 30 seconds"),
    (60, "Every minute"),
    (300, "Every 5 minutes"),
    (600, "Every 10 minutes"),
    (1800, "Every 30 minutes"),
    (0, "Manual only"),
)

DEFAULT_SYNC_INTERVAL = 60

#: IMAP host -> (SMTP host, port, security) for the providers people actually use.
_SMTP_GUESSES: dict[str, tuple[str, int, str]] = {
    "imap.gmail.com": ("smtp.gmail.com", 587, "starttls"),
    "outlook.office365.com": ("smtp.office365.com", 587, "starttls"),
    "imap-mail.outlook.com": ("smtp-mail.outlook.com", 587, "starttls"),
    "imap.mail.yahoo.com": ("smtp.mail.yahoo.com", 587, "starttls"),
    "imap.mail.me.com": ("smtp.mail.me.com", 587, "starttls"),
    "imap.zoho.com": ("smtp.zoho.com", 587, "starttls"),
    "imap.yandex.com": ("smtp.yandex.com", 465, "ssl"),
    "imap.mail.ru": ("smtp.mail.ru", 465, "ssl"),
}


def default_config_path() -> Path:
    return Path(__file__).resolve().with_name("config.ini")


def default_download_dir() -> Path:
    return Path(__file__).resolve().with_name("downloads")


def default_cache_dir() -> Path:
    return Path(__file__).resolve().with_name("cache")


@dataclass
class AccountSettings:
    """Everything needed to open an IMAP session."""

    host: str = DEFAULT_IMAP_SERVER
    port: int = DEFAULT_IMAP_PORT
    username: str = ""
    password: str = ""
    use_ssl: bool = True
    starttls: bool = False
    folder: str = "INBOX"
    timeout: int = 30

    @property
    def is_complete(self) -> bool:
        return bool(self.host and self.username and self.password)

    def redacted(self) -> dict[str, object]:
        """Safe to log."""
        return {"host": self.host, "port": self.port, "username": self.username,
                "ssl": self.use_ssl, "starttls": self.starttls, "folder": self.folder}


@dataclass
class SmtpSettings:
    """Outgoing mail.  Empty user/password means "reuse the IMAP ones"."""

    host: str = ""
    port: int = 587
    security: str = "starttls"       # "starttls", "ssl" or "none"
    username: str = ""
    password: str = ""
    from_name: str = ""
    timeout: int = 30

    def resolved(self, account: AccountSettings) -> "SmtpSettings":
        """Fill the blanks from the IMAP account / provider defaults."""
        host, port, security = self.host, self.port, self.security
        if not host:
            guess = _SMTP_GUESSES.get(account.host.lower())
            if guess:
                host, port, security = guess
            elif account.host.lower().startswith("imap."):
                host = "smtp." + account.host[5:]
        return SmtpSettings(
            host=host,
            port=port,
            security=security,
            username=self.username or account.username,
            password=self.password or account.password,
            from_name=self.from_name,
            timeout=self.timeout,
        )

    @property
    def is_complete(self) -> bool:
        return bool(self.host and self.username and self.password)

    def redacted(self) -> dict[str, object]:
        return {"host": self.host, "port": self.port, "security": self.security,
                "username": self.username}


@dataclass
class SyncSettings:
    """Automatic mailbox refresh."""

    interval_seconds: int = DEFAULT_SYNC_INTERVAL
    sync_on_start: bool = True
    cache_enabled: bool = True
    cache_dir: str = ""
    max_messages_per_folder: int = 200

    @property
    def is_automatic(self) -> bool:
        return self.interval_seconds > 0

    def cache_path(self) -> Path:
        return Path(self.cache_dir or default_cache_dir()).expanduser()


@dataclass
class WindowState:
    """Remembered geometry so the window opens where the user left it."""

    width: int = 1280
    height: int = 820
    x: int = -1
    y: int = -1
    maximized: bool = False
    splitter_sizes: str = ""          # comma separated pixel widths

    def sizes(self) -> list[int]:
        try:
            return [int(part) for part in self.splitter_sizes.split(",") if part.strip()]
        except ValueError:
            return []


@dataclass
class AppSettings:
    """User preferences plus the accounts."""

    account: AccountSettings = field(default_factory=AccountSettings)
    smtp: SmtpSettings = field(default_factory=SmtpSettings)
    sync: SyncSettings = field(default_factory=SyncSettings)
    window: WindowState = field(default_factory=WindowState)
    download_dir: str = ""
    fetch_limit: int = 50
    default_filter: str = "all"
    mark_seen: bool = False
    remember_password: bool = False
    allow_remote_images: bool = False
    theme: str = "system"             # "system", "light" or "dark"
    sort_key: str = "date"
    sort_descending: bool = True
    path: Path = field(default_factory=default_config_path)

    # ------------------------------------------------------------------ files
    @classmethod
    def load(cls, path: Path | str | None = None) -> "AppSettings":
        config_path = Path(path) if path else default_config_path()
        settings = cls(path=config_path)
        settings.download_dir = str(default_download_dir())
        settings.sync.cache_dir = str(default_cache_dir())

        parser = configparser.ConfigParser(interpolation=None)
        if config_path.exists():
            try:
                parser.read(config_path, encoding="utf-8")
            except Exception:
                logger.warning("Ignoring unreadable %s", config_path, exc_info=True)

        if parser.has_section("account"):
            section = parser["account"]
            account = settings.account
            account.host = section.get("host", account.host)
            account.port = section.getint("port", account.port)
            account.username = section.get("username", account.username)
            account.password = section.get("password", account.password)
            account.use_ssl = section.getboolean("use_ssl", account.use_ssl)
            account.starttls = section.getboolean("starttls", account.starttls)
            account.folder = section.get("folder", account.folder)
            account.timeout = section.getint("timeout", account.timeout)

        if parser.has_section("smtp"):
            section = parser["smtp"]
            smtp = settings.smtp
            smtp.host = section.get("host", smtp.host)
            smtp.port = section.getint("port", smtp.port)
            smtp.security = section.get("security", smtp.security)
            smtp.username = section.get("username", smtp.username)
            smtp.password = section.get("password", smtp.password)
            smtp.from_name = section.get("from_name", smtp.from_name)
            smtp.timeout = section.getint("timeout", smtp.timeout)

        if parser.has_section("sync"):
            section = parser["sync"]
            sync = settings.sync
            sync.interval_seconds = section.getint("interval_seconds", sync.interval_seconds)
            sync.sync_on_start = section.getboolean("sync_on_start", sync.sync_on_start)
            sync.cache_enabled = section.getboolean("cache_enabled", sync.cache_enabled)
            sync.cache_dir = section.get("cache_dir", sync.cache_dir)
            sync.max_messages_per_folder = section.getint(
                "max_messages_per_folder", sync.max_messages_per_folder
            )

        if parser.has_section("viewer"):
            section = parser["viewer"]
            settings.download_dir = section.get("download_dir", settings.download_dir)
            settings.fetch_limit = section.getint("fetch_limit", settings.fetch_limit)
            settings.default_filter = section.get("default_filter", settings.default_filter)
            settings.mark_seen = section.getboolean("mark_seen", settings.mark_seen)
            settings.remember_password = section.getboolean(
                "remember_password", settings.remember_password
            )
            settings.allow_remote_images = section.getboolean(
                "allow_remote_images", settings.allow_remote_images
            )
            settings.theme = section.get("theme", settings.theme)
            settings.sort_key = section.get("sort_key", settings.sort_key)
            settings.sort_descending = section.getboolean(
                "sort_descending", settings.sort_descending
            )

        if parser.has_section("window"):
            section = parser["window"]
            window = settings.window
            window.width = section.getint("width", window.width)
            window.height = section.getint("height", window.height)
            window.x = section.getint("x", window.x)
            window.y = section.getint("y", window.y)
            window.maximized = section.getboolean("maximized", window.maximized)
            window.splitter_sizes = section.get("splitter_sizes", window.splitter_sizes)

        settings._apply_environment()
        return settings

    def _apply_environment(self) -> None:
        env = os.environ
        self.account.host = env.get("IMAP_SERVER", self.account.host)
        self.account.username = env.get("MAIL_USERNAME", self.account.username)
        self.account.password = env.get("MAIL_PASSWORD", self.account.password)
        self.smtp.host = env.get("SMTP_SERVER", self.smtp.host)
        self.download_dir = env.get("MAIL_DOWNLOAD_DIR", self.download_dir)
        for name, setter in (("IMAP_PORT", "imap"), ("SMTP_PORT", "smtp")):
            value = env.get(name)
            if value and value.isdigit():
                if setter == "imap":
                    self.account.port = int(value)
                else:
                    self.smtp.port = int(value)

    def save(self) -> None:
        """Persist settings.  Passwords are only stored if asked for."""
        parser = configparser.ConfigParser(interpolation=None)
        keep = self.remember_password
        parser["account"] = {
            "host": self.account.host,
            "port": str(self.account.port),
            "username": self.account.username,
            "password": self.account.password if keep else "",
            "use_ssl": str(self.account.use_ssl),
            "starttls": str(self.account.starttls),
            "folder": self.account.folder,
            "timeout": str(self.account.timeout),
        }
        parser["smtp"] = {
            "host": self.smtp.host,
            "port": str(self.smtp.port),
            "security": self.smtp.security,
            "username": self.smtp.username,
            "password": self.smtp.password if keep else "",
            "from_name": self.smtp.from_name,
            "timeout": str(self.smtp.timeout),
        }
        parser["sync"] = {
            "interval_seconds": str(self.sync.interval_seconds),
            "sync_on_start": str(self.sync.sync_on_start),
            "cache_enabled": str(self.sync.cache_enabled),
            "cache_dir": self.sync.cache_dir,
            "max_messages_per_folder": str(self.sync.max_messages_per_folder),
        }
        parser["viewer"] = {
            "download_dir": self.download_dir,
            "fetch_limit": str(self.fetch_limit),
            "default_filter": self.default_filter,
            "mark_seen": str(self.mark_seen),
            "remember_password": str(self.remember_password),
            "allow_remote_images": str(self.allow_remote_images),
            "theme": self.theme,
            "sort_key": self.sort_key,
            "sort_descending": str(self.sort_descending),
        }
        parser["window"] = {
            "width": str(self.window.width),
            "height": str(self.window.height),
            "x": str(self.window.x),
            "y": str(self.window.y),
            "maximized": str(self.window.maximized),
            "splitter_sizes": self.window.splitter_sizes,
        }
        try:
            with open(self.path, "w", encoding="utf-8") as handle:
                parser.write(handle)
            logger.info("Settings written to %s", self.path)
        except OSError:
            logger.warning("Could not write %s", self.path, exc_info=True)

    # ---------------------------------------------------------------- helpers
    def download_path(self) -> Path:
        folder = Path(self.download_dir or default_download_dir()).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def smtp_settings(self) -> SmtpSettings:
        """SMTP settings with provider defaults and IMAP credentials filled in."""
        return self.smtp.resolved(self.account)

    def account_key(self) -> str:
        """Stable identifier used for cache directories."""
        safe = "".join(c if c.isalnum() or c in "-_.@" else "_"
                       for c in f"{self.account.username}@{self.account.host}")
        return safe or "default"


def load_settings(path: Path | str | None = None) -> AppSettings:
    return AppSettings.load(path)
