"""Application settings: several accounts, sync, sending, viewer and window state.

Layout of ``config.ini``::

    [accounts]
    names = Personal|Work        ; "|" separated so names may contain commas
    active = Personal

    [account:Personal]           ; IMAP
    [smtp:Personal]              ; SMTP for the same profile
    [sync] [viewer] [window]     ; shared by every account

A file written by the previous single-account version (a plain ``[account]`` /
``[smtp]`` pair) is migrated automatically into one profile, so nothing is lost.

Settings come from three places, later ones winning:

1. the defaults below (Gmail, as in the original notebook),
2. ``config.ini``,
3. environment variables (``MAIL_USERNAME``, ``MAIL_PASSWORD``, ``IMAP_SERVER``,
   ``IMAP_PORT``, ``SMTP_SERVER``, ``SMTP_PORT``, ``MAIL_DOWNLOAD_DIR``), which
   apply to the *active* profile.

Passwords are only written to disk when the user asks for it.
"""

from __future__ import annotations

import configparser
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "AccountSettings",
    "SmtpSettings",
    "AccountProfile",
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

_NAME_SEPARATOR = "|"


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
    #: Try the other standard port/encryption pair when the first one times out.
    auto_port_fallback: bool = True

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
            auto_port_fallback=self.auto_port_fallback,
        )

    def alternatives(self) -> list[tuple[int, str]]:
        """Other (port, security) pairs worth trying when a connect times out."""
        pairs = [(587, "starttls"), (465, "ssl"), (25, "none")]
        return [pair for pair in pairs if pair != (self.port, self.security)]

    @property
    def is_complete(self) -> bool:
        return bool(self.host and self.username and self.password)

    def redacted(self) -> dict[str, object]:
        return {"host": self.host, "port": self.port, "security": self.security,
                "username": self.username}


@dataclass
class AccountProfile:
    """One mail account: a name, its IMAP settings and its SMTP settings."""

    name: str = "Default"
    account: AccountSettings = field(default_factory=AccountSettings)
    smtp: SmtpSettings = field(default_factory=SmtpSettings)
    enabled: bool = True

    @property
    def key(self) -> str:
        """Identifier of the *mailbox*, used for cache and index directories.

        Deliberately built from the address and server rather than the profile
        name: renaming an account must not orphan its cache and force every
        message to be downloaded again.
        """
        safe = "".join(c if c.isalnum() or c in "-_.@" else "_"
                       for c in f"{self.account.username}@{self.account.host}")
        return safe or "default"

    @property
    def legacy_keys(self) -> tuple[str, ...]:
        """Directory names earlier versions used, so their cache can be adopted."""
        def clean(value: str) -> str:
            return "".join(c if c.isalnum() or c in "-_.@" else "_" for c in value)

        return tuple(dict.fromkeys(
            candidate for candidate in (
                clean(f"{self.name}-{self.account.username}"),
                clean(self.account.username),
            ) if candidate and candidate != self.key
        ))

    @property
    def display(self) -> str:
        return self.name or self.account.username or "Account"

    def smtp_settings(self) -> SmtpSettings:
        return self.smtp.resolved(self.account)

    def identity(self) -> str:
        """``Name <address>`` used as the From header."""
        smtp = self.smtp_settings()
        address = smtp.username or self.account.username
        return f"{smtp.from_name} <{address}>" if smtp.from_name else address


@dataclass
class SyncSettings:
    """Automatic mailbox refresh (shared by all accounts)."""

    interval_seconds: int = DEFAULT_SYNC_INTERVAL
    sync_on_start: bool = True
    cache_enabled: bool = True
    cache_dir: str = ""
    max_messages_per_folder: int = 200
    #: How often a queued (unsent) message is retried, in seconds.
    outbox_retry_seconds: int = 120

    @property
    def is_automatic(self) -> bool:
        return self.interval_seconds > 0

    def cache_path(self) -> Path:
        return Path(self.cache_dir or default_cache_dir()).expanduser()


@dataclass
class NotificationSettings:
    """Desktop notifications for newly arrived mail."""

    enabled: bool = True
    only_inbox: bool = True
    play_sound: bool = False
    #: Never show more than this many message lines in one notification.
    max_preview: int = 3
    #: Closing the window hides it to the tray instead of quitting, so new mail
    #: can still be announced.  The tray menu always offers a real Quit.
    minimize_to_tray: bool = True


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
    """User preferences plus every configured account."""

    profiles: list[AccountProfile] = field(default_factory=lambda: [AccountProfile()])
    active_profile: str = ""
    sync: SyncSettings = field(default_factory=SyncSettings)
    notifications: NotificationSettings = field(default_factory=NotificationSettings)
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

    # ------------------------------------------------------------- profiles
    @property
    def profile(self) -> AccountProfile:
        """The profile the UI is currently showing."""
        for profile in self.profiles:
            if profile.name == self.active_profile:
                return profile
        if not self.profiles:
            self.profiles.append(AccountProfile())
        self.active_profile = self.profiles[0].name
        return self.profiles[0]

    @property
    def account(self) -> AccountSettings:
        """IMAP settings of the active profile (kept for older call sites)."""
        return self.profile.account

    @property
    def smtp(self) -> SmtpSettings:
        return self.profile.smtp

    def smtp_settings(self, profile: Optional[AccountProfile] = None) -> SmtpSettings:
        """SMTP settings with provider defaults and IMAP credentials filled in."""
        return (profile or self.profile).smtp_settings()

    def account_key(self, profile: Optional[AccountProfile] = None) -> str:
        return (profile or self.profile).key

    def find_profile(self, name: str) -> Optional[AccountProfile]:
        return next((p for p in self.profiles if p.name == name), None)

    def profile_for_address(self, address: str) -> Optional[AccountProfile]:
        """Match an e-mail address back to the profile that owns it."""
        wanted = (address or "").strip().lower()
        for profile in self.profiles:
            candidates = {profile.account.username.lower(),
                          (profile.smtp.username or "").lower()}
            if wanted and wanted in candidates:
                return profile
        return None

    def unique_profile_name(self, base: str = "Account") -> str:
        existing = {p.name for p in self.profiles}
        if base not in existing:
            return base
        index = 2
        while f"{base} {index}" in existing:
            index += 1
        return f"{base} {index}"

    def add_profile(self, profile: AccountProfile) -> AccountProfile:
        profile.name = profile.name or self.unique_profile_name()
        if self.find_profile(profile.name) is not None:
            profile.name = self.unique_profile_name(profile.name)
        self.profiles.append(profile)
        if not self.active_profile:
            self.active_profile = profile.name
        return profile

    def remove_profile(self, name: str) -> bool:
        profile = self.find_profile(name)
        if profile is None or len(self.profiles) <= 1:
            return False
        self.profiles.remove(profile)
        if self.active_profile == name:
            self.active_profile = self.profiles[0].name
        return True

    def enabled_profiles(self) -> list[AccountProfile]:
        return [p for p in self.profiles if p.enabled and p.account.is_complete]

    # ------------------------------------------------------------------ files
    @classmethod
    def load(cls, path: Path | str | None = None) -> "AppSettings":
        config_path = Path(path) if path else default_config_path()
        settings = cls(profiles=[], path=config_path)
        settings.download_dir = str(default_download_dir())
        settings.sync.cache_dir = str(default_cache_dir())

        parser = configparser.ConfigParser(interpolation=None)
        if config_path.exists():
            try:
                parser.read(config_path, encoding="utf-8")
            except Exception:
                logger.warning("Ignoring unreadable %s", config_path, exc_info=True)

        names = _profile_names(parser)
        for name in names:
            settings.profiles.append(_read_profile(parser, name))
        if not settings.profiles:
            settings.profiles.append(AccountProfile(name="Default"))

        settings.active_profile = (
            parser.get("accounts", "active", fallback="") or settings.profiles[0].name
        )
        if settings.find_profile(settings.active_profile) is None:
            settings.active_profile = settings.profiles[0].name

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
            sync.outbox_retry_seconds = section.getint(
                "outbox_retry_seconds", sync.outbox_retry_seconds
            )

        if parser.has_section("notifications"):
            section = parser["notifications"]
            note = settings.notifications
            note.enabled = section.getboolean("enabled", note.enabled)
            note.only_inbox = section.getboolean("only_inbox", note.only_inbox)
            note.play_sound = section.getboolean("play_sound", note.play_sound)
            note.max_preview = section.getint("max_preview", note.max_preview)
            note.minimize_to_tray = section.getboolean(
                "minimize_to_tray", note.minimize_to_tray
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
        """Environment variables override the *active* profile."""
        env = os.environ
        profile = self.profile
        profile.account.host = env.get("IMAP_SERVER", profile.account.host)
        profile.account.username = env.get("MAIL_USERNAME", profile.account.username)
        profile.account.password = env.get("MAIL_PASSWORD", profile.account.password)
        profile.smtp.host = env.get("SMTP_SERVER", profile.smtp.host)
        self.download_dir = env.get("MAIL_DOWNLOAD_DIR", self.download_dir)
        imap_port = env.get("IMAP_PORT")
        if imap_port and imap_port.isdigit():
            profile.account.port = int(imap_port)
        smtp_port = env.get("SMTP_PORT")
        if smtp_port and smtp_port.isdigit():
            profile.smtp.port = int(smtp_port)

    def save(self) -> None:
        """Persist settings.  Passwords are only stored if asked for."""
        parser = configparser.ConfigParser(interpolation=None)
        keep = self.remember_password

        parser["accounts"] = {
            "names": _NAME_SEPARATOR.join(p.name for p in self.profiles),
            "active": self.active_profile or (self.profiles[0].name if self.profiles else ""),
        }
        for profile in self.profiles:
            parser[f"account:{profile.name}"] = {
                "host": profile.account.host,
                "port": str(profile.account.port),
                "username": profile.account.username,
                "password": profile.account.password if keep else "",
                "use_ssl": str(profile.account.use_ssl),
                "starttls": str(profile.account.starttls),
                "folder": profile.account.folder,
                "timeout": str(profile.account.timeout),
                "enabled": str(profile.enabled),
            }
            parser[f"smtp:{profile.name}"] = {
                "host": profile.smtp.host,
                "port": str(profile.smtp.port),
                "security": profile.smtp.security,
                "username": profile.smtp.username,
                "password": profile.smtp.password if keep else "",
                "from_name": profile.smtp.from_name,
                "timeout": str(profile.smtp.timeout),
                "auto_port_fallback": str(profile.smtp.auto_port_fallback),
            }

        parser["sync"] = {
            "interval_seconds": str(self.sync.interval_seconds),
            "sync_on_start": str(self.sync.sync_on_start),
            "cache_enabled": str(self.sync.cache_enabled),
            "cache_dir": self.sync.cache_dir,
            "max_messages_per_folder": str(self.sync.max_messages_per_folder),
            "outbox_retry_seconds": str(self.sync.outbox_retry_seconds),
        }
        parser["notifications"] = {
            "enabled": str(self.notifications.enabled),
            "only_inbox": str(self.notifications.only_inbox),
            "play_sound": str(self.notifications.play_sound),
            "max_preview": str(self.notifications.max_preview),
            "minimize_to_tray": str(self.notifications.minimize_to_tray),
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

    def outbox_path(self) -> Path:
        folder = self.sync.cache_path() / "outbox"
        folder.mkdir(parents=True, exist_ok=True)
        return folder


def _profile_names(parser: configparser.ConfigParser) -> list[str]:
    """Profile names, migrating a single-account file if that is what we have."""
    raw = parser.get("accounts", "names", fallback="").strip()
    if raw:
        names = [name.strip() for name in raw.split(_NAME_SEPARATOR) if name.strip()]
        if names:
            return names

    # Older format: one [account] (+ optional [smtp]) section.
    if parser.has_section("account"):
        username = parser.get("account", "username", fallback="").strip()
        name = username or "Default"
        parser[f"account:{name}"] = dict(parser["account"])
        if parser.has_section("smtp"):
            parser[f"smtp:{name}"] = dict(parser["smtp"])
        logger.info("Migrated the single-account configuration to profile %r", name)
        return [name]
    return []


def _read_profile(parser: configparser.ConfigParser, name: str) -> AccountProfile:
    profile = AccountProfile(name=name)
    section_name = f"account:{name}"
    if parser.has_section(section_name):
        section = parser[section_name]
        account = profile.account
        account.host = section.get("host", account.host)
        account.port = section.getint("port", account.port)
        account.username = section.get("username", account.username)
        account.password = section.get("password", account.password)
        account.use_ssl = section.getboolean("use_ssl", account.use_ssl)
        account.starttls = section.getboolean("starttls", account.starttls)
        account.folder = section.get("folder", account.folder)
        account.timeout = section.getint("timeout", account.timeout)
        profile.enabled = section.getboolean("enabled", True)

    smtp_section = f"smtp:{name}"
    if parser.has_section(smtp_section):
        section = parser[smtp_section]
        smtp = profile.smtp
        smtp.host = section.get("host", smtp.host)
        smtp.port = section.getint("port", smtp.port)
        smtp.security = section.get("security", smtp.security)
        smtp.username = section.get("username", smtp.username)
        smtp.password = section.get("password", smtp.password)
        smtp.from_name = section.get("from_name", smtp.from_name)
        smtp.timeout = section.getint("timeout", smtp.timeout)
        smtp.auto_port_fallback = section.getboolean(
            "auto_port_fallback", smtp.auto_port_fallback
        )
    return profile


def load_settings(path: Path | str | None = None) -> AppSettings:
    return AppSettings.load(path)
