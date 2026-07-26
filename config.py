"""Application settings: IMAP account, download folder, viewer preferences.

Settings come from three places, later ones winning:

1. the defaults below (Gmail, as in the original notebook),
2. ``config.ini`` next to this file,
3. environment variables (``MAIL_USERNAME``, ``MAIL_PASSWORD``, ``IMAP_SERVER``,
   ``IMAP_PORT``, ``MAIL_DOWNLOAD_DIR``).

Passwords are only written to ``config.ini`` when the user explicitly asks for
it; the environment (or the login dialog) is the recommended route.
"""

from __future__ import annotations

import configparser
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["AccountSettings", "AppSettings", "default_config_path", "load_settings"]

DEFAULT_IMAP_SERVER = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993


def default_config_path() -> Path:
    return Path(__file__).resolve().with_name("config.ini")


def default_download_dir() -> Path:
    return Path(__file__).resolve().with_name("downloads")


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

    @property
    def is_complete(self) -> bool:
        return bool(self.host and self.username and self.password)


@dataclass
class AppSettings:
    """User preferences plus the account."""

    account: AccountSettings = field(default_factory=AccountSettings)
    download_dir: str = ""
    fetch_limit: int = 50
    default_filter: str = "unread"
    mark_seen: bool = False
    remember_password: bool = False
    allow_remote_images: bool = False
    path: Path = field(default_factory=default_config_path)

    # ------------------------------------------------------------------ files
    @classmethod
    def load(cls, path: Path | str | None = None) -> "AppSettings":
        config_path = Path(path) if path else default_config_path()
        settings = cls(path=config_path)
        settings.download_dir = str(default_download_dir())

        parser = configparser.ConfigParser()
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

        settings._apply_environment()
        return settings

    def _apply_environment(self) -> None:
        env = os.environ
        self.account.host = env.get("IMAP_SERVER", self.account.host)
        self.account.username = env.get("MAIL_USERNAME", self.account.username)
        self.account.password = env.get("MAIL_PASSWORD", self.account.password)
        self.download_dir = env.get("MAIL_DOWNLOAD_DIR", self.download_dir)
        port = env.get("IMAP_PORT")
        if port and port.isdigit():
            self.account.port = int(port)

    def save(self) -> None:
        """Persist settings.  The password is only stored if asked for."""
        parser = configparser.ConfigParser()
        parser["account"] = {
            "host": self.account.host,
            "port": str(self.account.port),
            "username": self.account.username,
            "password": self.account.password if self.remember_password else "",
            "use_ssl": str(self.account.use_ssl),
            "starttls": str(self.account.starttls),
            "folder": self.account.folder,
        }
        parser["viewer"] = {
            "download_dir": self.download_dir,
            "fetch_limit": str(self.fetch_limit),
            "default_filter": self.default_filter,
            "mark_seen": str(self.mark_seen),
            "remember_password": str(self.remember_password),
            "allow_remote_images": str(self.allow_remote_images),
        }
        try:
            with open(self.path, "w", encoding="utf-8") as handle:
                parser.write(handle)
            logger.info("Settings written to %s", self.path)
        except OSError:
            logger.warning("Could not write %s", self.path, exc_info=True)

    def download_path(self) -> Path:
        folder = Path(self.download_dir or default_download_dir()).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        return folder


def load_settings(path: Path | str | None = None) -> AppSettings:
    return AppSettings.load(path)
