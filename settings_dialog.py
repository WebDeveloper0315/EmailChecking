"""Settings dialog: account, sending, synchronisation and appearance.

The connection test runs in a worker thread - the dialog stays responsive and
reports the same user-friendly errors the rest of the app uses.
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
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from config import SYNC_INTERVALS, AppSettings  # noqa: E402
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
        except ReceiveError as exc:
            self.finished.emit(False, f"IMAP failed:\n{exc}")
            return
        except Exception as exc:
            self.finished.emit(False, f"IMAP failed:\n{exc}")
            return

        if self._smtp is not None and self._smtp.is_complete:
            import smtplib
            import ssl

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

        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)

        tabs = QTabWidget()
        tabs.addTab(self._build_account_tab(), "Account")
        tabs.addTab(self._build_sending_tab(), "Sending")
        tabs.addTab(self._build_sync_tab(), "Synchronisation")
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

    # ------------------------------------------------------------------ tabs
    def _build_account_tab(self) -> QWidget:
        account = self._settings.account
        widget = QWidget()
        form = QFormLayout(widget)

        self._imap_host = QLineEdit(account.host)
        self._imap_port = QSpinBox()
        self._imap_port.setRange(1, 65535)
        self._imap_port.setValue(account.port)
        self._username = QLineEdit(account.username)
        self._password = QLineEdit(account.password)
        self._password.setEchoMode(QLineEdit.Password)
        self._imap_ssl = QCheckBox("Use SSL (usually port 993)")
        self._imap_ssl.setChecked(account.use_ssl)
        self._imap_starttls = QCheckBox("Use STARTTLS (usually port 143)")
        self._imap_starttls.setChecked(account.starttls)
        self._timeout = QSpinBox()
        self._timeout.setRange(5, 300)
        self._timeout.setValue(account.timeout)
        self._timeout.setSuffix(" s")
        self._remember = QCheckBox("Remember passwords in config.ini (stored in clear text)")
        self._remember.setChecked(self._settings.remember_password)
        self._mark_seen = QCheckBox("Mark messages as read when they are downloaded")
        self._mark_seen.setChecked(self._settings.mark_seen)

        form.addRow("IMAP server:", self._imap_host)
        form.addRow("Port:", self._imap_port)
        form.addRow("User name:", self._username)
        form.addRow("Password:", self._password)
        form.addRow("", self._imap_ssl)
        form.addRow("", self._imap_starttls)
        form.addRow("Timeout:", self._timeout)
        form.addRow("", self._mark_seen)
        form.addRow("", self._remember)

        hint = QLabel(
            "Gmail, Outlook and Yahoo require an <b>app password</b>.<br>"
            "You can also set <code>MAIL_USERNAME</code> and <code>MAIL_PASSWORD</code> "
            "as environment variables instead of storing them here."
        )
        hint.setWordWrap(True)
        form.addRow("", hint)
        return widget

    def _build_sending_tab(self) -> QWidget:
        smtp = self._settings.smtp
        resolved = self._settings.smtp_settings()
        widget = QWidget()
        layout = QVBoxLayout(widget)

        box = QGroupBox("SMTP server")
        form = QFormLayout(box)
        self._smtp_host = QLineEdit(smtp.host)
        self._smtp_host.setPlaceholderText(resolved.host or "smtp.example.com")
        self._smtp_port = QSpinBox()
        self._smtp_port.setRange(1, 65535)
        self._smtp_port.setValue(smtp.port or resolved.port)
        self._smtp_security = QComboBox()
        for value, label in (("starttls", "STARTTLS (port 587)"),
                             ("ssl", "SSL/TLS (port 465)"),
                             ("none", "No encryption")):
            self._smtp_security.addItem(label, value)
        index = self._smtp_security.findData(smtp.security or resolved.security)
        self._smtp_security.setCurrentIndex(max(0, index))
        self._smtp_user = QLineEdit(smtp.username)
        self._smtp_user.setPlaceholderText("same as the IMAP user name")
        self._smtp_password = QLineEdit(smtp.password)
        self._smtp_password.setEchoMode(QLineEdit.Password)
        self._smtp_password.setPlaceholderText("same as the IMAP password")
        self._from_name = QLineEdit(smtp.from_name)
        self._from_name.setPlaceholderText("shown as the sender name")

        form.addRow("SMTP server:", self._smtp_host)
        form.addRow("Port:", self._smtp_port)
        form.addRow("Encryption:", self._smtp_security)
        form.addRow("User name:", self._smtp_user)
        form.addRow("Password:", self._smtp_password)
        form.addRow("Display name:", self._from_name)

        layout.addWidget(box)
        note = QLabel(
            "Leave the server empty to use the provider default derived from the "
            "IMAP server (for example imap.gmail.com → smtp.gmail.com)."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)
        return widget

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
        form.addRow("", self._cache_enabled)
        form.addRow("Cache folder:", cache_row)
        form.addRow("Download folder:", download_row)
        return widget

    def _build_appearance_tab(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)

        self._theme = QComboBox()
        for value, label in (("system", "Follow the system"), ("light", "Light"), ("dark", "Dark")):
            self._theme.addItem(label, value)
        index = self._theme.findData(self._settings.theme)
        self._theme.setCurrentIndex(max(0, index))

        self._remote_images = QCheckBox("Load remote images by default (allows tracking)")
        self._remote_images.setChecked(self._settings.allow_remote_images)

        self._sort_key = QComboBox()
        for value, label in (("date", "Date"), ("from", "Sender"), ("subject", "Subject"),
                             ("size", "Size"), ("unread", "Unread first")):
            self._sort_key.addItem(label, value)
        index = self._sort_key.findData(self._settings.sort_key)
        self._sort_key.setCurrentIndex(max(0, index))

        self._sort_descending = QCheckBox("Newest / largest first")
        self._sort_descending.setChecked(self._settings.sort_descending)

        form.addRow("Theme:", self._theme)
        form.addRow("Sort by:", self._sort_key)
        form.addRow("", self._sort_descending)
        form.addRow("", self._remote_images)

        note = QLabel("The window size and position are remembered automatically.")
        note.setWordWrap(True)
        form.addRow("", note)
        return widget

    def _browse_into(self, target: QLineEdit) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose folder", target.text() or str(Path.home()))
        if folder:
            target.setText(folder)

    # ------------------------------------------------------------- actions
    def _collect(self) -> None:
        """Copy the widgets back into the settings object."""
        account = self._settings.account
        account.host = self._imap_host.text().strip()
        account.port = self._imap_port.value()
        account.username = self._username.text().strip()
        account.password = self._password.text()
        account.use_ssl = self._imap_ssl.isChecked()
        account.starttls = self._imap_starttls.isChecked()
        account.timeout = self._timeout.value()

        smtp = self._settings.smtp
        smtp.host = self._smtp_host.text().strip()
        smtp.port = self._smtp_port.value()
        smtp.security = self._smtp_security.currentData()
        smtp.username = self._smtp_user.text().strip()
        smtp.password = self._smtp_password.text()
        smtp.from_name = self._from_name.text().strip()

        sync = self._settings.sync
        sync.interval_seconds = int(self._interval.currentData())
        sync.sync_on_start = self._sync_on_start.isChecked()
        sync.max_messages_per_folder = self._max_messages.value()
        sync.cache_enabled = self._cache_enabled.isChecked()
        sync.cache_dir = self._cache_dir.text().strip()

        self._settings.download_dir = self._download_dir.text().strip()
        self._settings.remember_password = self._remember.isChecked()
        self._settings.mark_seen = self._mark_seen.isChecked()
        self._settings.theme = self._theme.currentData()
        self._settings.allow_remote_images = self._remote_images.isChecked()
        self._settings.sort_key = self._sort_key.currentData()
        self._settings.sort_descending = self._sort_descending.isChecked()

        register_secret(account.password)
        register_secret(smtp.password)

    def _test_connection(self) -> None:
        if self._thread is not None:
            return
        self._collect()
        self._test_button.setEnabled(False)
        self._test_result.setText("Testing…")

        self._thread = QThread(self)
        self._tester = _ConnectionTester(self._settings.account, self._settings.smtp_settings())
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
                           "interval": self._settings.sync.interval_seconds,
                           "theme": self._settings.theme,
                           **self._settings.account.redacted()})
        super().accept()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(2000)
        super().closeEvent(event)
