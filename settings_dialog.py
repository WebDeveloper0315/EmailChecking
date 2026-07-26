"""Settings dialog: accounts, sending, synchronisation, notifications, appearance.

The Accounts tab manages a *list* of profiles: selecting one loads its IMAP and
SMTP fields, and edits are committed back to the profile whenever the selection
changes or the dialog is accepted.  The connection test runs in a worker thread
so the dialog stays responsive and reports the same user-friendly errors the
rest of the application uses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import qt_bootstrap

qt_bootstrap.prepare()

from PySide6.QtCore import QObject, Qt, QThread, Signal  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from config import SYNC_INTERVALS, AccountProfile, AppSettings  # noqa: E402
from logging_setup import get_logger, register_secret  # noqa: E402

logger = get_logger("config", "mail.settings")

__all__ = ["SettingsDialog"]


class _ConnectionTester(QObject):
    """Tries IMAP and (optionally) SMTP login, off the UI thread."""

    finished = Signal(bool, str)

    def __init__(self, account, smtp=None) -> None:
        super().__init__()
        self._account = account
        self._smtp = smtp

    def run(self) -> None:
        from mail_receiver import ImapClient, ReceiveError

        messages: list[str] = []
        try:
            client = ImapClient(self._account)
            client.connect()
            folders = client.list_folders()
            client.logout()
            messages.append(f"IMAP: connected, {len(folders)} folder(s) found.")
        except (ReceiveError, Exception) as exc:
            self.finished.emit(False, f"IMAP failed:\n{exc}")
            return

        if self._smtp is not None and self._smtp.is_complete:
            import smtplib
            import ssl

            from mail_sender import diagnose_unreachable

            try:
                if self._smtp.security == "ssl":
                    server = smtplib.SMTP_SSL(self._smtp.host, self._smtp.port,
                                              timeout=self._smtp.timeout,
                                              context=ssl.create_default_context())
                else:
                    server = smtplib.SMTP(self._smtp.host, self._smtp.port,
                                          timeout=self._smtp.timeout)
                server.ehlo()
                if self._smtp.security == "starttls":
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                server.login(self._smtp.username, self._smtp.password)
                server.quit()
                messages.append("SMTP: login accepted.")
            except (TimeoutError, OSError) as exc:
                detail = diagnose_unreachable(self._smtp.host)
                self.finished.emit(
                    False, "\n".join(messages) + f"\n\nSMTP failed: {exc}\n{detail}"
                )
                return
            except Exception as exc:
                self.finished.emit(False, "\n".join(messages) + f"\n\nSMTP failed:\n{exc}")
                return
        else:
            messages.append("SMTP: not configured, skipped.")

        self.finished.emit(True, "\n".join(messages))


class SettingsDialog(QDialog):
    """Edits an :class:`AppSettings` in place; ``exec()`` returns Accepted."""

    def __init__(self, settings: AppSettings, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._thread: Optional[QThread] = None
        self._tester: Optional[_ConnectionTester] = None
        self._editing: Optional[AccountProfile] = None

        self.setWindowTitle("Settings")
        self.setMinimumSize(720, 560)

        tabs = QTabWidget()
        tabs.addTab(self._build_accounts_tab(), "Accounts")
        tabs.addTab(self._build_sync_tab(), "Synchronisation")
        tabs.addTab(self._build_notifications_tab(), "Notifications")
        tabs.addTab(self._build_appearance_tab(), "Appearance")

        self._test_button = QPushButton("Test connection")
        self._test_button.clicked.connect(self._test_connection)
        self._test_result = QLabel()
        self._test_result.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        bottom = QHBoxLayout()
        bottom.addWidget(self._test_button)
        bottom.addStretch(1)
        bottom.addWidget(buttons)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(self._test_result)
        layout.addLayout(bottom)

        self._reload_account_list(select=settings.active_profile)

    # ------------------------------------------------------------- accounts
    def _build_accounts_tab(self) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)

        # ---- left: the list of accounts
        left = QVBoxLayout()
        self._account_list = QListWidget()
        self._account_list.setMaximumWidth(190)
        self._account_list.currentItemChanged.connect(self._on_account_changed)
        left.addWidget(self._account_list)

        buttons = QHBoxLayout()
        add_button = QPushButton("Add")
        add_button.clicked.connect(self._add_account)
        remove_button = QPushButton("Remove")
        remove_button.clicked.connect(self._remove_account)
        rename_button = QPushButton("Rename")
        rename_button.clicked.connect(self._rename_account)
        for button in (add_button, remove_button, rename_button):
            buttons.addWidget(button)
        left.addLayout(buttons)
        layout.addLayout(left)

        # ---- right: the selected account's servers
        right = QVBoxLayout()

        incoming = QGroupBox("Incoming mail (IMAP)")
        form = QFormLayout(incoming)
        self._imap_host = QLineEdit()
        self._imap_port = QSpinBox()
        self._imap_port.setRange(1, 65535)
        self._username = QLineEdit()
        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.Password)
        self._imap_ssl = QCheckBox("Use SSL (usually port 993)")
        self._imap_starttls = QCheckBox("Use STARTTLS (usually port 143)")
        self._timeout = QSpinBox()
        self._timeout.setRange(5, 300)
        self._timeout.setSuffix(" s")
        self._enabled = QCheckBox("Synchronise this account")
        form.addRow("Server:", self._imap_host)
        form.addRow("Port:", self._imap_port)
        form.addRow("User name:", self._username)
        form.addRow("Password:", self._password)
        form.addRow("", self._imap_ssl)
        form.addRow("", self._imap_starttls)
        form.addRow("Timeout:", self._timeout)
        form.addRow("", self._enabled)
        right.addWidget(incoming)

        outgoing = QGroupBox("Outgoing mail (SMTP)")
        smtp_form = QFormLayout(outgoing)
        self._smtp_host = QLineEdit()
        self._smtp_host.setPlaceholderText("leave empty to derive from the IMAP server")
        self._smtp_port = QSpinBox()
        self._smtp_port.setRange(1, 65535)
        self._smtp_security = QComboBox()
        for value, label in (("starttls", "STARTTLS (port 587)"),
                             ("ssl", "SSL/TLS (port 465)"),
                             ("none", "No encryption")):
            self._smtp_security.addItem(label, value)
        self._smtp_user = QLineEdit()
        self._smtp_user.setPlaceholderText("same as the IMAP user name")
        self._smtp_password = QLineEdit()
        self._smtp_password.setEchoMode(QLineEdit.Password)
        self._smtp_password.setPlaceholderText("same as the IMAP password")
        self._from_name = QLineEdit()
        self._from_name.setPlaceholderText("shown as the sender name")
        self._port_fallback = QCheckBox(
            "If the port times out, try the other standard ports (465 / 587 / 25)"
        )
        smtp_form.addRow("Server:", self._smtp_host)
        smtp_form.addRow("Port:", self._smtp_port)
        smtp_form.addRow("Encryption:", self._smtp_security)
        smtp_form.addRow("User name:", self._smtp_user)
        smtp_form.addRow("Password:", self._smtp_password)
        smtp_form.addRow("Display name:", self._from_name)
        smtp_form.addRow("", self._port_fallback)
        right.addWidget(outgoing)

        self._remember = QCheckBox("Remember passwords in config.ini (stored in clear text)")
        self._mark_seen = QCheckBox("Mark messages as read when they are opened")
        right.addWidget(self._remember)
        right.addWidget(self._mark_seen)

        hint = QLabel(
            "Gmail, Outlook and Yahoo require an <b>app password</b>. If sending times "
            "out on every port, a VPN or the provider is usually blocking SMTP."
        )
        hint.setWordWrap(True)
        right.addWidget(hint)
        right.addStretch(1)
        layout.addLayout(right, 1)
        return widget

    def _reload_account_list(self, select: str = "") -> None:
        self._account_list.blockSignals(True)
        self._account_list.clear()
        for profile in self._settings.profiles:
            item = QListWidgetItem(profile.display)
            item.setData(Qt.UserRole, profile.name)
            self._account_list.addItem(item)
        self._account_list.blockSignals(False)

        target = select or (self._settings.profiles[0].name if self._settings.profiles else "")
        for row in range(self._account_list.count()):
            if self._account_list.item(row).data(Qt.UserRole) == target:
                self._account_list.setCurrentRow(row)
                return
        if self._account_list.count():
            self._account_list.setCurrentRow(0)

    def _on_account_changed(self, current: Optional[QListWidgetItem],
                            previous: Optional[QListWidgetItem]) -> None:
        if previous is not None and self._editing is not None:
            self._commit_profile(self._editing)
        if current is None:
            self._editing = None
            return
        profile = self._settings.find_profile(str(current.data(Qt.UserRole)))
        self._editing = profile
        if profile is not None:
            self._load_profile(profile)

    def _load_profile(self, profile: AccountProfile) -> None:
        account, smtp = profile.account, profile.smtp
        self._imap_host.setText(account.host)
        self._imap_port.setValue(account.port)
        self._username.setText(account.username)
        self._password.setText(account.password)
        self._imap_ssl.setChecked(account.use_ssl)
        self._imap_starttls.setChecked(account.starttls)
        self._timeout.setValue(account.timeout)
        self._enabled.setChecked(profile.enabled)

        resolved = profile.smtp_settings()
        self._smtp_host.setText(smtp.host)
        self._smtp_host.setPlaceholderText(resolved.host or "smtp.example.com")
        self._smtp_port.setValue(smtp.port or resolved.port)
        index = self._smtp_security.findData(smtp.security or resolved.security)
        self._smtp_security.setCurrentIndex(max(0, index))
        self._smtp_user.setText(smtp.username)
        self._smtp_password.setText(smtp.password)
        self._from_name.setText(smtp.from_name)
        self._port_fallback.setChecked(smtp.auto_port_fallback)

        self._remember.setChecked(self._settings.remember_password)
        self._mark_seen.setChecked(self._settings.mark_seen)

    def _commit_profile(self, profile: AccountProfile) -> None:
        account, smtp = profile.account, profile.smtp
        account.host = self._imap_host.text().strip()
        account.port = self._imap_port.value()
        account.username = self._username.text().strip()
        account.password = self._password.text()
        account.use_ssl = self._imap_ssl.isChecked()
        account.starttls = self._imap_starttls.isChecked()
        account.timeout = self._timeout.value()
        profile.enabled = self._enabled.isChecked()

        smtp.host = self._smtp_host.text().strip()
        smtp.port = self._smtp_port.value()
        smtp.security = self._smtp_security.currentData()
        smtp.username = self._smtp_user.text().strip()
        smtp.password = self._smtp_password.text()
        smtp.from_name = self._from_name.text().strip()
        smtp.auto_port_fallback = self._port_fallback.isChecked()

        register_secret(account.password)
        register_secret(smtp.password)

    def _add_account(self) -> None:
        if self._editing is not None:
            self._commit_profile(self._editing)
        name, ok = QInputDialog.getText(self, "Add account", "Name for this account:",
                                        text=self._settings.unique_profile_name())
        if not ok or not name.strip():
            return
        profile = self._settings.add_profile(AccountProfile(name=name.strip()))
        self._reload_account_list(select=profile.name)

    def _remove_account(self) -> None:
        if self._editing is None:
            return
        if len(self._settings.profiles) <= 1:
            QMessageBox.information(self, "Settings",
                                    "At least one account is needed.")
            return
        answer = QMessageBox.question(
            self, "Remove account",
            f"Remove “{self._editing.display}”?\n\n"
            "Only the local configuration and cache are affected; nothing is "
            "deleted on the server.",
        )
        if answer != QMessageBox.Yes:
            return
        name = self._editing.name
        self._editing = None
        self._settings.remove_profile(name)
        self._reload_account_list(select=self._settings.active_profile)

    def _rename_account(self) -> None:
        if self._editing is None:
            return
        name, ok = QInputDialog.getText(self, "Rename account", "New name:",
                                        text=self._editing.name)
        name = name.strip()
        if not ok or not name or name == self._editing.name:
            return
        if self._settings.find_profile(name) is not None:
            QMessageBox.warning(self, "Rename account", "That name is already used.")
            return
        was_active = self._settings.active_profile == self._editing.name
        self._editing.name = name
        if was_active:
            self._settings.active_profile = name
        self._reload_account_list(select=name)

    # ----------------------------------------------------------------- tabs
    def _build_sync_tab(self) -> QWidget:
        sync = self._settings.sync
        widget = QWidget()
        form = QFormLayout(widget)

        self._interval = QComboBox()
        for seconds, label in SYNC_INTERVALS:
            self._interval.addItem(label, seconds)
        index = self._interval.findData(sync.interval_seconds)
        if index < 0:                       # a custom value from config.ini
            self._interval.addItem(f"Every {sync.interval_seconds} seconds",
                                   sync.interval_seconds)
            index = self._interval.count() - 1
        self._interval.setCurrentIndex(index)

        self._sync_on_start = QCheckBox("Synchronise when the application starts")
        self._sync_on_start.setChecked(sync.sync_on_start)
        self._max_messages = QSpinBox()
        self._max_messages.setRange(10, 5000)
        self._max_messages.setSingleStep(10)
        self._max_messages.setValue(sync.max_messages_per_folder)
        self._cache_enabled = QCheckBox("Keep downloaded messages on disk")
        self._cache_enabled.setChecked(sync.cache_enabled)
        self._outbox_retry = QSpinBox()
        self._outbox_retry.setRange(30, 3600)
        self._outbox_retry.setSingleStep(30)
        self._outbox_retry.setSuffix(" s")
        self._outbox_retry.setValue(sync.outbox_retry_seconds)

        cache_row = QWidget()
        cache_layout = QHBoxLayout(cache_row)
        cache_layout.setContentsMargins(0, 0, 0, 0)
        self._cache_dir = QLineEdit(sync.cache_dir)
        browse_cache = QPushButton("Browse…")
        browse_cache.clicked.connect(lambda: self._browse_into(self._cache_dir))
        cache_layout.addWidget(self._cache_dir)
        cache_layout.addWidget(browse_cache)

        download_row = QWidget()
        download_layout = QHBoxLayout(download_row)
        download_layout.setContentsMargins(0, 0, 0, 0)
        self._download_dir = QLineEdit(self._settings.download_dir)
        browse_download = QPushButton("Browse…")
        browse_download.clicked.connect(lambda: self._browse_into(self._download_dir))
        download_layout.addWidget(self._download_dir)
        download_layout.addWidget(browse_download)

        form.addRow("Refresh:", self._interval)
        form.addRow("", self._sync_on_start)
        form.addRow("Messages per folder:", self._max_messages)
        form.addRow("Retry unsent mail every:", self._outbox_retry)
        form.addRow("", self._cache_enabled)
        form.addRow("Cache folder:", cache_row)
        form.addRow("Download folder:", download_row)
        return widget

    def _build_notifications_tab(self) -> QWidget:
        note = self._settings.notifications
        widget = QWidget()
        form = QFormLayout(widget)

        self._notify_enabled = QCheckBox("Show a notification when new mail arrives")
        self._notify_enabled.setChecked(note.enabled)
        self._notify_inbox_only = QCheckBox("Only for the Inbox")
        self._notify_inbox_only.setChecked(note.only_inbox)
        self._notify_sound = QCheckBox("Play a sound")
        self._notify_sound.setChecked(note.play_sound)
        self._notify_tray = QCheckBox(
            "Keep running in the tray when the window is closed"
        )
        self._notify_tray.setChecked(note.minimize_to_tray)
        self._notify_preview = QSpinBox()
        self._notify_preview.setRange(1, 10)
        self._notify_preview.setValue(note.max_preview)

        form.addRow("", self._notify_enabled)
        form.addRow("", self._notify_inbox_only)
        form.addRow("", self._notify_sound)
        form.addRow("", self._notify_tray)
        form.addRow("Messages listed per notification:", self._notify_preview)

        hint = QLabel(
            "Notifications appear for messages that arrive <i>after</i> a folder has "
            "been loaded once, so the first synchronisation stays quiet. Clicking a "
            "notification opens that message."
        )
        hint.setWordWrap(True)
        form.addRow("", hint)
        return widget

    def _build_appearance_tab(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)

        self._theme = QComboBox()
        for value, label in (("system", "Follow the system"), ("light", "Light"),
                             ("dark", "Dark")):
            self._theme.addItem(label, value)
        self._theme.setCurrentIndex(max(0, self._theme.findData(self._settings.theme)))

        self._remote_images = QCheckBox("Load remote images by default (allows tracking)")
        self._remote_images.setChecked(self._settings.allow_remote_images)

        self._sort_key = QComboBox()
        for value, label in (("date", "Date"), ("from", "Sender"), ("subject", "Subject"),
                             ("size", "Size"), ("unread", "Unread first")):
            self._sort_key.addItem(label, value)
        self._sort_key.setCurrentIndex(max(0, self._sort_key.findData(self._settings.sort_key)))

        self._sort_descending = QCheckBox("Newest / largest first")
        self._sort_descending.setChecked(self._settings.sort_descending)

        form.addRow("Theme:", self._theme)
        form.addRow("Sort by:", self._sort_key)
        form.addRow("", self._sort_descending)
        form.addRow("", self._remote_images)

        note = QLabel("The window size and position are remembered automatically. "
                      "Changing the theme takes effect after a restart.")
        note.setWordWrap(True)
        form.addRow("", note)
        return widget

    def _browse_into(self, target: QLineEdit) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose folder",
                                                  target.text() or str(Path.home()))
        if folder:
            target.setText(folder)

    # ------------------------------------------------------------- actions
    def _collect(self) -> None:
        """Copy the widgets back into the settings object."""
        if self._editing is not None:
            self._commit_profile(self._editing)

        sync = self._settings.sync
        sync.interval_seconds = int(self._interval.currentData())
        sync.sync_on_start = self._sync_on_start.isChecked()
        sync.max_messages_per_folder = self._max_messages.value()
        sync.cache_enabled = self._cache_enabled.isChecked()
        sync.cache_dir = self._cache_dir.text().strip()
        sync.outbox_retry_seconds = self._outbox_retry.value()

        note = self._settings.notifications
        note.enabled = self._notify_enabled.isChecked()
        note.only_inbox = self._notify_inbox_only.isChecked()
        note.play_sound = self._notify_sound.isChecked()
        note.minimize_to_tray = self._notify_tray.isChecked()
        note.max_preview = self._notify_preview.value()

        self._settings.download_dir = self._download_dir.text().strip()
        self._settings.remember_password = self._remember.isChecked()
        self._settings.mark_seen = self._mark_seen.isChecked()
        self._settings.theme = self._theme.currentData()
        self._settings.allow_remote_images = self._remote_images.isChecked()
        self._settings.sort_key = self._sort_key.currentData()
        self._settings.sort_descending = self._sort_descending.isChecked()

    def _test_connection(self) -> None:
        if self._thread is not None or self._editing is None:
            return
        self._commit_profile(self._editing)
        self._test_button.setEnabled(False)
        self._test_result.setText(f"Testing {self._editing.display}…")

        self._thread = QThread(self)
        self._tester = _ConnectionTester(self._editing.account,
                                         self._editing.smtp_settings())
        self._tester.moveToThread(self._thread)
        self._thread.started.connect(self._tester.run)
        self._tester.finished.connect(self._on_test_finished)
        self._tester.finished.connect(self._thread.quit)
        self._thread.finished.connect(self._tester.deleteLater)
        self._thread.start()

    def _on_test_finished(self, ok: bool, message: str) -> None:
        self._thread = None
        self._tester = None
        self._test_button.setEnabled(True)
        colour = "#137333" if ok else "#c5221f"
        prefix = "✓" if ok else "✗"
        self._test_result.setText(
            f'<span style="color:{colour}">{prefix} {message.replace(chr(10), "<br>")}</span>'
        )

    def accept(self) -> None:
        self._collect()
        self._settings.save()
        logger.info("Settings updated",
                    extra={"event": "settings_saved",
                           "accounts": len(self._settings.profiles),
                           "interval": self._settings.sync.interval_seconds,
                           "notifications": self._settings.notifications.enabled,
                           "theme": self._settings.theme})
        super().accept()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(2000)
        super().closeEvent(event)
