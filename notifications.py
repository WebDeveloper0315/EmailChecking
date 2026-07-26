"""Desktop notifications and the system tray icon.

New mail produces a native toast ("2 new messages — Alice: Lunch?") and the tray
icon carries an unread badge.  Clicking a notification raises the window and
opens the message it was about.

The icon is drawn at runtime with QPainter, so the application needs no image
files and the badge can be re-rendered whenever the unread count changes.

Notifications are *suppressed for the first sync of a folder*: filling an empty
list with 200 existing messages is not "new mail", and 200 toasts would be a
punishment rather than a feature.
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence

import qt_bootstrap

qt_bootstrap.prepare()

from PySide6.QtCore import QObject, QRect, Qt, Signal  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QAction,
    QBrush,
    QColor,
    QFont,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon  # noqa: E402

from config import AppSettings  # noqa: E402
from logging_setup import get_logger  # noqa: E402
from models import Email  # noqa: E402

logger = get_logger("ui", "mail.notifications")

__all__ = ["NotificationCenter", "make_mail_icon"]

_ENVELOPE = QColor("#1a73e8")
_BADGE = QColor("#d93025")


def make_mail_icon(unread: int = 0, size: int = 64) -> QIcon:
    """An envelope, with a red unread badge when there is something to read."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)

    margin = size // 8
    body = QRect(margin, margin + size // 10, size - 2 * margin, size - 2 * margin - size // 5)
    painter.setBrush(QBrush(_ENVELOPE))
    painter.setPen(QPen(_ENVELOPE.darker(120), max(1, size // 32)))
    painter.drawRoundedRect(body, size // 16, size // 16)

    # The flap: two lines from the top corners to the middle.
    painter.setPen(QPen(QColor("#ffffff"), max(1, size // 20)))
    painter.drawLine(body.left() + size // 16, body.top() + size // 16,
                     body.center().x(), body.center().y())
    painter.drawLine(body.right() - size // 16, body.top() + size // 16,
                     body.center().x(), body.center().y())

    if unread > 0:
        diameter = int(size * 0.5)
        badge = QRect(size - diameter - 1, 0, diameter, diameter)
        painter.setBrush(QBrush(_BADGE))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(badge)

        text = "99+" if unread > 99 else str(unread)
        font = QFont()
        font.setBold(True)
        font.setPixelSize(int(diameter * (0.5 if len(text) > 2 else 0.66)))
        painter.setFont(font)
        painter.setPen(QPen(QColor("#ffffff")))
        painter.drawText(badge, int(Qt.AlignCenter), text)

    painter.end()
    return QIcon(pixmap)


class NotificationCenter(QObject):
    """Owns the tray icon and turns "new mail" into toasts."""

    #: (account, folder, uid) of the message a clicked notification referred to.
    message_activated = Signal(str, str, str)
    open_requested = Signal()
    sync_requested = Signal()
    quit_requested = Signal()

    def __init__(self, settings: AppSettings, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._unread = 0
        self._last_target: tuple[str, str, str] = ("", "", "")
        #: Folders whose first sync has already been seen, per account.
        self._primed: set[tuple[str, str]] = set()

        self._tray: Optional[QSystemTrayIcon] = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray = QSystemTrayIcon(make_mail_icon(0), self)
            self._tray.setToolTip("Mail Viewer")
            self._tray.activated.connect(self._on_activated)
            self._tray.messageClicked.connect(self._on_message_clicked)
            self._tray.setContextMenu(self._build_menu())
            self._tray.show()
        else:  # pragma: no cover - depends on the desktop environment
            logger.info("No system tray available; notifications are disabled",
                        extra={"event": "tray_unavailable"})

    # ------------------------------------------------------------------ menu
    def _build_menu(self) -> QMenu:
        menu = QMenu()
        open_action = QAction("Open Mail Viewer", menu)
        open_action.triggered.connect(self.open_requested.emit)
        sync_action = QAction("Sync now", menu)
        sync_action.triggered.connect(self.sync_requested.emit)
        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self.quit_requested.emit)
        menu.addAction(open_action)
        menu.addAction(sync_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        return menu

    def _on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.open_requested.emit()

    def _on_message_clicked(self) -> None:
        account, folder, uid = self._last_target
        self.open_requested.emit()
        if uid:
            self.message_activated.emit(account, folder, uid)

    # --------------------------------------------------------------- public
    @property
    def available(self) -> bool:
        return self._tray is not None

    def prime(self, account: str, folder: str) -> bool:
        """Mark a folder as "seen once".

        Returns True if this was the first time, i.e. the caller should *not*
        notify about the messages of that pass.
        """
        key = (account, folder)
        first_time = key not in self._primed
        self._primed.add(key)
        return first_time

    def is_primed(self, account: str, folder: str) -> bool:
        return (account, folder) in self._primed

    def forget(self, account: str) -> None:
        """Drop the primed state of an account (used when it is removed)."""
        self._primed = {key for key in self._primed if key[0] != account}

    def set_unread(self, unread: int, per_account: Optional[dict[str, int]] = None) -> None:
        """Update the badge and the tray tooltip."""
        unread = max(0, int(unread))
        if self._tray is None:
            self._unread = unread
            return
        if unread != self._unread:
            self._tray.setIcon(make_mail_icon(unread))
        self._unread = unread

        lines = ["Mail Viewer"]
        if per_account:
            for name, count in sorted(per_account.items()):
                lines.append(f"{name}: {count} unread")
        elif unread:
            lines.append(f"{unread} unread")
        self._tray.setToolTip("\n".join(lines))

    def notify_new_messages(self, account: str, folder: str,
                            messages: Sequence[Email]) -> bool:
        """Show a toast for freshly arrived mail.  Returns True if shown."""
        settings = self._settings.notifications
        if not settings.enabled or self._tray is None or not messages:
            return False
        if settings.only_inbox and folder.upper() != "INBOX" and "inbox" not in folder.lower():
            return False

        newest = messages[0]
        self._last_target = (account, folder, newest.uid)

        prefix = f"{account} · " if len(self._settings.profiles) > 1 else ""
        if len(messages) == 1:
            title = f"{prefix}New message"
            body = f"{newest.sender_short}\n{newest.display_subject}"
        else:
            title = f"{prefix}{len(messages)} new messages"
            preview = messages[: max(1, settings.max_preview)]
            body = "\n".join(f"{m.sender_short}: {m.display_subject}" for m in preview)
            if len(messages) > len(preview):
                body += f"\n… and {len(messages) - len(preview)} more"

        try:
            self._tray.showMessage(title, body, make_mail_icon(0), 8000)
        except Exception:
            logger.debug("Could not show a notification", exc_info=True)
            return False

        if settings.play_sound:
            application = QApplication.instance()
            if application is not None:
                application.beep()

        logger.info("Notified about %d new message(s)", len(messages),
                    extra={"event": "notify", "account": account, "folder": folder,
                           "count": len(messages)})
        return True

    def notify(self, title: str, body: str) -> None:
        """Generic notification (send failures, outbox retries …)."""
        if self._tray is None or not self._settings.notifications.enabled:
            return
        self._last_target = ("", "", "")
        try:
            self._tray.showMessage(title, body, make_mail_icon(0), 6000)
        except Exception:
            logger.debug("Could not show a notification", exc_info=True)

    def shutdown(self) -> None:
        if self._tray is not None:
            self._tray.hide()
            self._tray.deleteLater()
            self._tray = None
