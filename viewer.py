"""PySide6 desktop viewer.

Layout
------
+--------------------------------------------------------------+
| toolbar: filter | limit | Fetch | Open .eml | Account | search |
+-------------------+------------------------------------------+
| message list      | header block (From/To/Cc/Subject/Date...) |
| (sender, subject, +------------------------------------------+
|  preview, date)   | body (QWebEngineView, else QTextBrowser)  |
|                   +------------------------------------------+
|                   | attachments                              |
+-------------------+------------------------------------------+

Downloading and parsing happen in a worker thread; the UI thread only ever
receives finished :class:`~models.Email` objects, so a 20 MB message never
freezes the window.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

import qt_bootstrap

qt_bootstrap.prepare()

from PySide6.QtCore import (  # noqa: E402
    QAbstractListModel,
    QByteArray,
    QModelIndex,
    QObject,
    QRect,
    QSize,
    QSortFilterProxyModel,
    Qt,
    QThread,
    QUrl,
    Signal,
)
from PySide6.QtGui import (  # noqa: E402
    QAction,
    QColor,
    QDesktopServices,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QKeySequence,
    QPainter,
    QPalette,
    QPixmap,
)
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTextBrowser,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from attachment_manager import (  # noqa: E402
    human_size,
    make_data_uri_resolver,
    make_passthrough_resolver,
    sanitize_filename,
    save_all,
    save_attachment,
)
from config import AppSettings  # noqa: E402
from html_processor import SanitizedHtml, build_document, sanitize_html, text_to_html  # noqa: E402
from mail_parser import find_inline_attachment, parse_email  # noqa: E402
from mail_receiver import FILTERS, EmlFileSource, ImboxReceiver, ReceiveError  # noqa: E402
from models import Attachment, Email, format_addresses  # noqa: E402

logger = logging.getLogger(__name__)

APP_NAME = "Mail Viewer"

# QtWebEngine is optional: it renders mail HTML far better, but it is a large
# component and is missing from some installs.  The import must happen before
# a QApplication exists, hence at module import time.  Set
# MAILVIEWER_NO_WEBENGINE=1 to force the QTextBrowser renderer.
try:
    if os.environ.get("MAILVIEWER_NO_WEBENGINE") == "1":
        raise ImportError("disabled by MAILVIEWER_NO_WEBENGINE")
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
    from PySide6.QtWebEngineWidgets import QWebEngineView

    WEBENGINE_AVAILABLE = True
except Exception as _exc:  # pragma: no cover - depends on environment
    logger.info("QtWebEngine unavailable (%s); using QTextBrowser instead", _exc)
    WEBENGINE_AVAILABLE = False

EMAIL_ROLE = int(Qt.UserRole) + 1
SEARCH_ROLE = int(Qt.UserRole) + 2


# --------------------------------------------------------------------- worker
class FetchWorker(QObject):
    """Downloads and parses messages off the UI thread."""

    message_ready = Signal(object)   # Email
    progress = Signal(int)           # messages parsed so far
    finished = Signal(int, str)      # count, error ("" when fine)

    def __init__(self, account, criteria: str, limit: int, mark_seen: bool) -> None:
        super().__init__()
        self._account = account
        self._criteria = criteria
        self._limit = limit
        self._mark_seen = mark_seen
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        count, error = 0, ""
        receiver = ImboxReceiver(self._account)
        try:
            for raw in receiver.fetch(self._criteria, self._limit, lambda: self._stop):
                mail = parse_email(raw.raw, uid=raw.uid, source=raw.folder)
                count += 1
                self.message_ready.emit(mail)
                self.progress.emit(count)
                if self._mark_seen:
                    receiver.mark_seen(raw.uid)
                if self._stop:
                    break
        except ReceiveError as exc:
            error = str(exc)
        except Exception as exc:  # never let the thread die silently
            logger.exception("Unexpected error while fetching")
            error = f"Unexpected error: {exc}"
        finally:
            receiver.logout()
        self.finished.emit(count, error)


class FileLoadWorker(QObject):
    """Parses ``.eml`` files off the UI thread."""

    message_ready = Signal(object)
    finished = Signal(int, str)

    def __init__(self, paths: list[str]) -> None:
        super().__init__()
        self._paths = paths

    def run(self) -> None:
        count, error = 0, ""
        try:
            for raw in EmlFileSource.read(self._paths):
                self.message_ready.emit(parse_email(raw.raw, uid=raw.uid, source=raw.folder))
                count += 1
        except Exception as exc:
            logger.exception("Failed to load .eml files")
            error = str(exc)
        self.finished.emit(count, error)


# ---------------------------------------------------------------------- model
class MessageListModel(QAbstractListModel):
    """Holds the fetched messages, newest first."""

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._emails: list[Email] = []
        self._blobs: dict[int, str] = {}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._emails)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._emails):
            return None
        mail = self._emails[index.row()]
        if role == Qt.DisplayRole:
            return mail.display_subject
        if role == EMAIL_ROLE:
            return mail
        if role == SEARCH_ROLE:
            key = id(mail)
            if key not in self._blobs:
                self._blobs[key] = mail.search_blob()
            return self._blobs[key]
        if role == Qt.ToolTipRole:
            return (f"{mail.sender}\n{mail.display_subject}\n"
                    f"{mail.display_date}\n\n{mail.preview(200)}")
        return None

    def add(self, mail: Email) -> None:
        """Insert keeping the list sorted by date, newest first."""
        position = len(self._emails)
        for i, existing in enumerate(self._emails):
            if mail.sort_key > existing.sort_key:
                position = i
                break
        self.beginInsertRows(QModelIndex(), position, position)
        self._emails.insert(position, mail)
        self.endInsertRows()

    def clear(self) -> None:
        self.beginResetModel()
        self._emails.clear()
        self._blobs.clear()
        self.endResetModel()

    def email_at(self, row: int) -> Optional[Email]:
        return self._emails[row] if 0 <= row < len(self._emails) else None

    @property
    def emails(self) -> list[Email]:
        return list(self._emails)


class MessageDelegate(QStyledItemDelegate):
    """Three-line row: sender + date, subject, preview - like a mail client."""

    PADDING = 8

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        metrics = QFontMetrics(option.font)
        return QSize(240, metrics.height() * 3 + self.PADDING * 2 + 4)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        mail: Optional[Email] = index.data(EMAIL_ROLE)
        if mail is None:
            super().paint(painter, option, index)
            return

        painter.save()
        selected = bool(option.state & QStyle.State_Selected)
        palette = option.palette
        if selected:
            painter.fillRect(option.rect, palette.highlight())
            primary = palette.highlightedText().color()
            secondary = QColor(primary)
            secondary.setAlpha(190)
        else:
            painter.fillRect(option.rect, palette.base())
            primary = palette.text().color()
            secondary = palette.color(QPalette.Disabled, QPalette.Text)

        rect = option.rect.adjusted(self.PADDING, self.PADDING, -self.PADDING, -self.PADDING)
        metrics = QFontMetrics(option.font)
        line_height = metrics.height()

        bold = QFont(option.font)
        bold.setBold(True)
        small = QFont(option.font)
        small.setPointSizeF(max(7.0, option.font.pointSizeF() - 1))
        small_metrics = QFontMetrics(small)

        # Line 1: sender (bold, left) and date (small, right).
        date_text = mail.display_date
        date_width = small_metrics.horizontalAdvance(date_text) + 6
        painter.setFont(small)
        painter.setPen(secondary)
        painter.drawText(
            QRect(rect.right() - date_width, rect.top(), date_width, line_height),
            int(Qt.AlignRight | Qt.AlignVCenter),
            date_text,
        )
        painter.setFont(bold)
        painter.setPen(primary)
        sender_rect = QRect(rect.left(), rect.top(), rect.width() - date_width - 6, line_height)
        painter.drawText(
            sender_rect,
            int(Qt.AlignLeft | Qt.AlignVCenter),
            QFontMetrics(bold).elidedText(mail.sender_short, Qt.ElideRight, sender_rect.width()),
        )

        # Line 2: attachment icon (drawn, not an emoji - fonts vary) + subject.
        painter.setFont(option.font)
        subject = mail.display_subject
        subject_left = rect.left()
        if mail.has_attachments:
            icon_size = line_height - 4
            icon = QApplication.style().standardIcon(QStyle.SP_FileIcon)
            icon.paint(
                painter,
                QRect(rect.left(), rect.top() + line_height + 2, icon_size, icon_size),
                Qt.AlignCenter,
            )
            subject_left += icon_size + 4
        subject_rect = QRect(subject_left, rect.top() + line_height,
                             rect.right() - subject_left, line_height)
        painter.drawText(
            subject_rect,
            int(Qt.AlignLeft | Qt.AlignVCenter),
            metrics.elidedText(subject, Qt.ElideRight, subject_rect.width()),
        )

        # Line 3: preview.
        painter.setFont(small)
        painter.setPen(secondary)
        preview_rect = QRect(rect.left(), rect.top() + line_height * 2, rect.width(), line_height)
        painter.drawText(
            preview_rect,
            int(Qt.AlignLeft | Qt.AlignVCenter),
            small_metrics.elidedText(mail.preview(160), Qt.ElideRight, preview_rect.width()),
        )

        painter.restore()


# ------------------------------------------------------------------ body view
class _MailTextBrowser(QTextBrowser):
    """Fallback renderer; resolves ``cid:`` images from the current message."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._mail: Optional[Email] = None
        self.setOpenExternalLinks(False)
        self.setOpenLinks(False)
        self.anchorClicked.connect(_open_external)

    def set_mail(self, mail: Optional[Email]) -> None:
        self._mail = mail

    def loadResource(self, resource_type: int, url: QUrl):  # noqa: N802 - Qt API
        if self._mail is not None and url.scheme() == "cid":
            attachment = find_inline_attachment(self._mail, url.path() or url.toString()[4:])
            if attachment is not None:
                try:
                    return QByteArray(attachment.data)
                except Exception:
                    logger.debug("Inline image failed to decode", exc_info=True)
        if url.scheme() in ("http", "https"):
            return QByteArray()  # never fetch remote content from the text view
        return super().loadResource(resource_type, url)


if WEBENGINE_AVAILABLE:

    _SHARED_PROFILE: Optional["QWebEngineProfile"] = None

    def _mail_profile() -> "QWebEngineProfile":
        """One off-the-record profile for the whole application.

        It must outlive every page that uses it (Qt prints "Release of profile
        requested but WebEnginePage still not deleted" otherwise), so it is
        owned by the QApplication rather than by a widget.
        """
        global _SHARED_PROFILE
        if _SHARED_PROFILE is None:
            _SHARED_PROFILE = QWebEngineProfile(QApplication.instance())
            _SHARED_PROFILE.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
            _SHARED_PROFILE.setPersistentCookiesPolicy(
                QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies
            )
        return _SHARED_PROFILE

    class _MailPage(QWebEnginePage):
        """Opens clicked links in the real browser; blocks in-place navigation."""

        def acceptNavigationRequest(self, url: QUrl, nav_type, is_main_frame: bool) -> bool:  # noqa: N802
            if nav_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
                _open_external(url)
                return False
            return True

        def javaScriptConsoleMessage(self, level, message, line, source) -> None:  # noqa: N802
            logger.debug("JS console (%s:%s): %s", source, line, message)


class BodyView(QWidget):
    """Renders a message body with whichever engine is available."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._mail: Optional[Email] = None
        self._allow_remote = False
        self._web: Optional[QWidget] = None
        self._text: Optional[_MailTextBrowser] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        if WEBENGINE_AVAILABLE:
            self._web = QWebEngineView(self)
            # Off-the-record profile: nothing about the mail you read is cached.
            page = _MailPage(_mail_profile(), self._web)
            self._web.setPage(page)
            settings = page.settings()
            for attribute, value in (
                (QWebEngineSettings.WebAttribute.JavascriptEnabled, False),
                (QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False),
                (QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, False),
                (QWebEngineSettings.WebAttribute.ErrorPageEnabled, False),
                (QWebEngineSettings.WebAttribute.PluginsEnabled, False),
                (QWebEngineSettings.WebAttribute.FullScreenSupportEnabled, False),
            ):
                try:
                    settings.setAttribute(attribute, value)
                except Exception:
                    logger.debug("Could not set web attribute %s", attribute)
            layout.addWidget(self._web)
        else:
            self._text = _MailTextBrowser(self)
            layout.addWidget(self._text)

    @property
    def backend(self) -> str:
        return "QtWebEngine" if self._web is not None else "QTextBrowser"

    def show_email(self, mail: Optional[Email], allow_remote_images: bool) -> SanitizedHtml:
        """Render a message; returns what the sanitiser had to block."""
        self._mail = mail
        self._allow_remote = allow_remote_images
        if mail is None:
            self._render("", SanitizedHtml(html=""))
            return SanitizedHtml(html="")

        if mail.has_html:
            resolver = (make_data_uri_resolver(mail) if self._web is not None
                        else make_passthrough_resolver(mail))
            result = sanitize_html(mail.html_body, resolver, allow_remote_images)
        elif mail.text_body:
            result = SanitizedHtml(html=text_to_html(mail.text_body))
        else:
            result = SanitizedHtml(
                html='<p style="color:#5f6368">This message has no readable body.</p>'
            )
        self._render(build_document(result.html, dark=_is_dark()), result)
        return result

    def _render(self, document: str, result: SanitizedHtml) -> None:
        if self._web is not None:
            # setContent avoids setHtml's ~2 MB limit (big inline images).
            self._web.page().setContent(
                QByteArray(document.encode("utf-8")), "text/html;charset=utf-8", QUrl("about:blank")
            )
        elif self._text is not None:
            self._text.set_mail(self._mail)
            self._text.setHtml(document)

    def find(self, text: str) -> None:
        if self._web is not None:
            self._web.page().findText(text)
        elif self._text is not None and text:
            if not self._text.find(text):
                cursor = self._text.textCursor()
                cursor.setPosition(0)
                self._text.setTextCursor(cursor)
                self._text.find(text)

    def copy_selection(self) -> None:
        if self._web is not None:
            self._web.page().triggerAction(QWebEnginePage.WebAction.Copy)
        elif self._text is not None:
            self._text.copy()


def _open_external(url: QUrl) -> None:
    if url.scheme() in ("http", "https", "mailto", "tel", "ftp", "ftps"):
        QDesktopServices.openUrl(url)
    else:
        logger.info("Refused to open link with scheme %r", url.scheme())


def _dim(label: QLabel, alpha: int = 150) -> None:
    """Grey out a label in a way that works in both light and dark themes.

    A ``color: palette(mid)`` stylesheet looks fine on a light theme and is
    unreadable on a dark one, so the colour is derived from the actual text
    colour instead.
    """
    palette = label.palette()
    colour = palette.color(QPalette.WindowText)
    colour.setAlpha(alpha)
    palette.setColor(QPalette.WindowText, colour)
    label.setPalette(palette)


def _is_dark() -> bool:
    app = QApplication.instance()
    if app is None:
        return False
    return app.palette().color(QPalette.Window).lightness() < 128


# ---------------------------------------------------------------- header pane
class HeaderPane(QWidget):
    """From / To / Cc / Subject / Date block, plus all raw headers on demand."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 6)
        layout.setSpacing(4)

        self._subject = QLabel()
        subject_font = self._subject.font()
        subject_font.setPointSizeF(subject_font.pointSizeF() + 3)
        subject_font.setBold(True)
        self._subject.setFont(subject_font)
        self._subject.setWordWrap(True)
        self._subject.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self._subject)

        self._form = QFormLayout()
        self._form.setContentsMargins(0, 4, 0, 0)
        self._form.setHorizontalSpacing(12)
        self._form.setVerticalSpacing(2)
        self._fields: dict[str, QLabel] = {}
        for name in ("From", "Reply-To", "To", "Cc", "Bcc", "Date", "Message-ID"):
            value = QLabel()
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            label = QLabel(f"{name}:")
            _dim(label)
            self._fields[name] = value
            self._form.addRow(label, value)
        layout.addLayout(self._form)

        self._warning = QLabel()
        self._warning.setWordWrap(True)
        self._warning.setStyleSheet(
            "background:#fff4d6; color:#7a5900; border:1px solid #f0d38a;"
            "border-radius:4px; padding:4px 8px;"
        )
        self._warning.hide()
        layout.addWidget(self._warning)

        controls = QHBoxLayout()
        self._details_button = QPushButton("Show all headers")
        self._details_button.setCheckable(True)
        self._details_button.setFlat(True)
        self._details_button.toggled.connect(self._toggle_details)
        controls.addWidget(self._details_button)
        controls.addStretch(1)
        layout.addLayout(controls)

        self._details = QPlainTextEdit()
        self._details.setReadOnly(True)
        self._details.setMaximumHeight(180)
        self._details.hide()
        layout.addWidget(self._details)

        self.clear()

    def clear(self) -> None:
        self._subject.setText("")
        for label in self._fields.values():
            label.setText("")
        self._warning.hide()
        self._details.setPlainText("")
        self._set_row_visibility({})

    def show_email(self, mail: Email) -> None:
        self._subject.setText(mail.display_subject)
        values = {
            "From": format_addresses(mail.from_addrs),
            "Reply-To": format_addresses(mail.reply_to),
            "To": format_addresses(mail.to_addrs),
            "Cc": format_addresses(mail.cc_addrs),
            "Bcc": format_addresses(mail.bcc_addrs),
            "Date": f"{mail.display_date}" + (f"   ({mail.date_raw})" if mail.date_raw else ""),
            "Message-ID": mail.message_id,
        }
        for name, text in values.items():
            self._fields[name].setText(text)
        self._set_row_visibility(values)

        self._details.setPlainText("\n".join(f"{name}: {value}" for name, value in mail.headers))

        if mail.warnings:
            self._warning.setText("⚠  " + "  •  ".join(mail.warnings[:4]))
            self._warning.show()
        else:
            self._warning.hide()

    def _set_row_visibility(self, values: dict[str, str]) -> None:
        for name, widget in self._fields.items():
            visible = bool(values.get(name)) or name in ("From", "To")
            label = self._form.labelForField(widget)
            widget.setVisible(visible)
            if label is not None:
                label.setVisible(visible)

    def _toggle_details(self, checked: bool) -> None:
        self._details.setVisible(checked)
        self._details_button.setText("Hide all headers" if checked else "Show all headers")


# ----------------------------------------------------------- attachment panel
class AttachmentPane(QWidget):
    """Bottom strip listing attachments, with saving."""

    def __init__(self, settings: AppSettings, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._mail: Optional[Email] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 8)
        layout.setSpacing(4)

        header = QHBoxLayout()
        self._title = QLabel("Attachments")
        self._title.setStyleSheet("font-weight: bold;")
        header.addWidget(self._title)
        header.addStretch(1)
        self._save_all_button = QPushButton("Save all…")
        self._save_all_button.clicked.connect(self.save_all)
        header.addWidget(self._save_all_button)
        layout.addLayout(header)

        self._list = QListWidget()
        self._list.setMaximumHeight(110)
        self._list.setAlternatingRowColors(True)
        self._list.setContextMenuPolicy(Qt.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._context_menu)
        self._list.itemDoubleClicked.connect(lambda _: self.save_selected())
        layout.addWidget(self._list)

        self.show_email(None)

    def show_email(self, mail: Optional[Email]) -> None:
        self._mail = mail
        self._list.clear()
        attachments = list(mail.attachments) if mail else []
        icon = self.style().standardIcon(QStyle.SP_FileIcon)
        for attachment in attachments:
            item = QListWidgetItem(
                icon,
                f"{attachment.filename}    {human_size(attachment.size)}    "
                f"{attachment.content_type}",
            )
            item.setData(Qt.UserRole, attachment)
            self._list.addItem(item)

        inline_count = len(mail.inline_images) if mail else 0
        if attachments:
            title = f"Attachments ({len(attachments)})"
        else:
            title = "Attachments (none)"
        if inline_count:
            title += f" · {inline_count} inline image(s)"
        self._title.setText(title)
        self._save_all_button.setEnabled(bool(attachments))
        self.setVisible(True)

    # ------------------------------------------------------------------ slots
    def _context_menu(self, position) -> None:
        item = self._list.itemAt(position)
        if item is None:
            return
        menu = QMenu(self)
        menu.addAction("Save as…", self.save_selected)
        menu.addAction("Save to downloads folder", self.save_selected_quick)
        attachment: Attachment = item.data(Qt.UserRole)
        if attachment.is_image:
            menu.addAction("Preview image", self.preview_selected)
        menu.exec(self._list.mapToGlobal(position))

    def _selected(self) -> Optional[Attachment]:
        item = self._list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def save_selected(self) -> None:
        attachment = self._selected()
        if attachment is None:
            return
        suggested = str(self._settings.download_path() / sanitize_filename(attachment.filename))
        path, _ = QFileDialog.getSaveFileName(self, "Save attachment", suggested)
        if not path:
            return
        try:
            Path(path).write_bytes(attachment.data)
            self._info(f"Saved to {path}")
        except Exception as exc:
            logger.exception("Saving attachment failed")
            QMessageBox.warning(self, APP_NAME, f"Could not save the attachment:\n{exc}")

    def save_selected_quick(self) -> None:
        attachment = self._selected()
        if attachment is None:
            return
        try:
            path = save_attachment(attachment, self._settings.download_path())
            self._info(f"Saved to {path}")
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, f"Could not save the attachment:\n{exc}")

    def save_all(self) -> None:
        if self._mail is None or not self._mail.attachments:
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Save all attachments", str(self._settings.download_path())
        )
        if not folder:
            return
        results = save_all(self._mail.attachments, folder)
        failures = [(a.filename, err) for a, path, err in results if path is None]
        if failures:
            details = "\n".join(f"• {name}: {err}" for name, err in failures)
            QMessageBox.warning(
                self, APP_NAME,
                f"Saved {len(results) - len(failures)} of {len(results)} attachments.\n\n"
                f"Failed:\n{details}",
            )
        else:
            self._info(f"Saved {len(results)} attachment(s) to {folder}")

    def preview_selected(self) -> None:
        attachment = self._selected()
        if attachment is None:
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(QByteArray(attachment.data)):
            QMessageBox.information(self, APP_NAME, "This image cannot be displayed.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(attachment.filename)
        layout = QVBoxLayout(dialog)
        label = QLabel()
        screen = QGuiApplication.primaryScreen().availableGeometry()
        label.setPixmap(
            pixmap.scaled(
                min(pixmap.width(), int(screen.width() * 0.8)),
                min(pixmap.height(), int(screen.height() * 0.8)),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        )
        layout.addWidget(label)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _info(self, message: str) -> None:
        window = self.window()
        if isinstance(window, QMainWindow):
            window.statusBar().showMessage(message, 6000)


# ---------------------------------------------------------------------- login
class AccountDialog(QDialog):
    """IMAP account settings."""

    def __init__(self, settings: AppSettings, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Mail account")
        self.setMinimumWidth(420)
        self._settings = settings
        account = settings.account

        form = QFormLayout()
        self._host = QLineEdit(account.host)
        self._port = QSpinBox()
        self._port.setRange(1, 65535)
        self._port.setValue(account.port)
        self._username = QLineEdit(account.username)
        self._password = QLineEdit(account.password)
        self._password.setEchoMode(QLineEdit.Password)
        self._folder = QLineEdit(account.folder)
        self._ssl = QCheckBox("Use SSL (port 993)")
        self._ssl.setChecked(account.use_ssl)
        self._starttls = QCheckBox("Use STARTTLS (port 143)")
        self._starttls.setChecked(account.starttls)
        self._remember = QCheckBox("Remember password in config.ini (stored in clear text)")
        self._remember.setChecked(settings.remember_password)
        self._mark_seen = QCheckBox("Mark messages as read after downloading")
        self._mark_seen.setChecked(settings.mark_seen)

        form.addRow("IMAP server:", self._host)
        form.addRow("Port:", self._port)
        form.addRow("User name:", self._username)
        form.addRow("Password:", self._password)
        form.addRow("Folder:", self._folder)
        form.addRow("", self._ssl)
        form.addRow("", self._starttls)
        form.addRow("", self._mark_seen)
        form.addRow("", self._remember)

        hint = QLabel(
            "Gmail and Outlook need an <b>app password</b>, not your normal one.<br>"
            "You can also set <code>MAIL_USERNAME</code> / <code>MAIL_PASSWORD</code> "
            "in the environment instead of saving them here."
        )
        hint.setWordWrap(True)
        _dim(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addWidget(buttons)

    def accept(self) -> None:
        account = self._settings.account
        account.host = self._host.text().strip()
        account.port = self._port.value()
        account.username = self._username.text().strip()
        account.password = self._password.text()
        account.folder = self._folder.text().strip() or "INBOX"
        account.use_ssl = self._ssl.isChecked()
        account.starttls = self._starttls.isChecked()
        self._settings.remember_password = self._remember.isChecked()
        self._settings.mark_seen = self._mark_seen.isChecked()
        self._settings.save()
        super().accept()


# ----------------------------------------------------------------- main window
class MainWindow(QMainWindow):
    def __init__(self, settings: AppSettings) -> None:
        super().__init__()
        self._settings = settings
        self._thread: Optional[QThread] = None
        self._worker: Optional[QObject] = None
        self._current: Optional[Email] = None
        self._allow_remote_for_current = settings.allow_remote_images

        self.setWindowTitle(APP_NAME)
        self.resize(1180, 760)

        self._model = MessageListModel(self)
        self._proxy = QSortFilterProxyModel(self)
        self._proxy.setSourceModel(self._model)
        self._proxy.setFilterRole(SEARCH_ROLE)
        self._proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)

        self._build_toolbar()
        self._build_central()
        self.statusBar().showMessage(f"Ready · rendering with {self._body.backend}")

    # ---------------------------------------------------------------- widgets
    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        self._filter = QComboBox()
        for value, label in FILTERS:
            self._filter.addItem(label, value)
        index = self._filter.findData(self._settings.default_filter)
        self._filter.setCurrentIndex(max(0, index))
        toolbar.addWidget(QLabel(" Show: "))
        toolbar.addWidget(self._filter)

        self._sender_filter = QLineEdit()
        self._sender_filter.setPlaceholderText("from: sender@example.com (optional)")
        self._sender_filter.setClearButtonEnabled(True)
        self._sender_filter.setMaximumWidth(240)
        toolbar.addWidget(self._sender_filter)

        toolbar.addWidget(QLabel("  Max: "))
        self._limit = QSpinBox()
        self._limit.setRange(1, 1000)
        self._limit.setValue(self._settings.fetch_limit)
        toolbar.addWidget(self._limit)
        toolbar.addSeparator()

        self._fetch_action = QAction("Fetch mail", self)
        self._fetch_action.setShortcut(QKeySequence("F5"))
        self._fetch_action.triggered.connect(self.fetch_mail)
        toolbar.addAction(self._fetch_action)

        self._stop_action = QAction("Stop", self)
        self._stop_action.setEnabled(False)
        self._stop_action.triggered.connect(self.stop_fetch)
        toolbar.addAction(self._stop_action)

        open_action = QAction("Open .eml…", self)
        open_action.setShortcut(QKeySequence.Open)
        open_action.triggered.connect(self.open_files)
        toolbar.addAction(open_action)

        account_action = QAction("Account…", self)
        account_action.triggered.connect(self.edit_account)
        toolbar.addAction(account_action)

        toolbar.addSeparator()
        self._images_action = QAction("Show remote images", self)
        self._images_action.setCheckable(True)
        self._images_action.setChecked(self._settings.allow_remote_images)
        self._images_action.toggled.connect(self._toggle_remote_images)
        toolbar.addAction(self._images_action)

        toolbar.addSeparator()
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search messages…  (Ctrl+F)")
        self._search.setClearButtonEnabled(True)
        self._search.setMaximumWidth(260)
        self._search.textChanged.connect(self._proxy.setFilterFixedString)
        toolbar.addWidget(self._search)

        search_shortcut = QAction(self)
        search_shortcut.setShortcut(QKeySequence.Find)
        search_shortcut.triggered.connect(self._search.setFocus)
        self.addAction(search_shortcut)

    def _build_central(self) -> None:
        splitter = QSplitter(Qt.Horizontal)

        self._list_view = QListView()
        self._list_view.setModel(self._proxy)
        self._list_view.setItemDelegate(MessageDelegate(self))
        self._list_view.setSelectionMode(QAbstractItemView.SingleSelection)
        self._list_view.setUniformItemSizes(True)
        self._list_view.setMinimumWidth(280)
        self._list_view.selectionModel().currentChanged.connect(self._on_selection)
        splitter.addWidget(self._list_view)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self._header = HeaderPane()
        right_layout.addWidget(self._header)

        self._banner = QLabel()
        self._banner.setWordWrap(True)
        self._banner.setOpenExternalLinks(False)
        self._banner.setStyleSheet(
            "background:#e8f0fe; color:#174ea6; padding:5px 12px; border-top:1px solid #c6dafc;"
        )
        self._banner.linkActivated.connect(lambda _: self._images_action.setChecked(True))
        self._banner.hide()
        right_layout.addWidget(self._banner)

        find_row = QWidget()
        find_layout = QHBoxLayout(find_row)
        find_layout.setContentsMargins(12, 4, 12, 4)
        find_layout.addWidget(QLabel("Find in message:"))
        self._body_find = QLineEdit()
        self._body_find.setPlaceholderText("type and press Enter")
        self._body_find.returnPressed.connect(lambda: self._body.find(self._body_find.text()))
        find_layout.addWidget(self._body_find)
        right_layout.addWidget(find_row)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        right_layout.addWidget(line)

        self._body = BodyView()
        right_layout.addWidget(self._body, 1)

        self._attachments = AttachmentPane(self._settings)
        right_layout.addWidget(self._attachments)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([330, 850])
        self.setCentralWidget(splitter)

    # ------------------------------------------------------------------ slots
    def _on_selection(self, current: QModelIndex, _previous: QModelIndex) -> None:
        mail: Optional[Email] = current.data(EMAIL_ROLE) if current.isValid() else None
        self._current = mail
        self._allow_remote_for_current = self._settings.allow_remote_images
        self._images_action.setChecked(self._allow_remote_for_current)
        self._render_current()

    def _render_current(self) -> None:
        mail = self._current
        if mail is None:
            self._header.clear()
            self._body.show_email(None, False)
            self._attachments.show_email(None)
            self._banner.hide()
            return

        self._header.show_email(mail)
        result = self._body.show_email(mail, self._allow_remote_for_current)
        self._attachments.show_email(mail)

        notes: list[str] = []
        if result.remote_images_blocked:
            notes.append(
                f"{result.remote_images_blocked} remote image(s) blocked to protect your "
                'privacy. <a href="#show">Show images</a>'
            )
        if result.trackers_removed:
            notes.append(f"{result.trackers_removed} tracking pixel(s) removed")
        if result.missing_inline_images:
            notes.append(f"{len(result.missing_inline_images)} inline image(s) not found")
        if notes:
            self._banner.setText(" · ".join(notes))
            self._banner.show()
        else:
            self._banner.hide()

        self.statusBar().showMessage(
            f"{mail.display_subject} · {human_size(mail.raw_size)} · "
            f"{len(mail.attachments)} attachment(s) · {self._body.backend}",
            8000,
        )

    def _toggle_remote_images(self, checked: bool) -> None:
        self._allow_remote_for_current = checked
        if self._current is not None:
            self._render_current()

    def edit_account(self) -> None:
        dialog = AccountDialog(self._settings, self)
        dialog.exec()

    # --------------------------------------------------------------- fetching
    def fetch_mail(self) -> None:
        if self._thread is not None:
            QMessageBox.information(self, APP_NAME, "A download is already running.")
            return

        account = self._settings.account
        if not account.username or not account.password:
            if AccountDialog(self._settings, self).exec() != QDialog.Accepted:
                return
            account = self._settings.account
            if not account.username or not account.password:
                return

        criteria = self._sender_filter.text().strip() or self._filter.currentData()
        self._settings.fetch_limit = self._limit.value()
        self._settings.default_filter = self._filter.currentData()

        self._model.clear()
        self._current = None
        self._render_current()

        worker = FetchWorker(account, criteria, self._limit.value(), self._settings.mark_seen)
        self._start_worker(worker, f"Downloading ({criteria})…")

    def open_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Open e-mail files", str(Path.home()),
            "E-mail files (*.eml *.msg *.txt);;All files (*)",
        )
        if not paths:
            return
        self._model.clear()
        self._start_worker(FileLoadWorker(paths), f"Loading {len(paths)} file(s)…")

    def _start_worker(self, worker: QObject, message: str) -> None:
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.message_ready.connect(self._on_message)
        worker.finished.connect(self._on_finished)
        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        self._thread, self._worker = thread, worker
        self._fetch_action.setEnabled(False)
        self._stop_action.setEnabled(hasattr(worker, "stop"))
        self.statusBar().showMessage(message)
        thread.start()

    def stop_fetch(self) -> None:
        if self._worker is not None and hasattr(self._worker, "stop"):
            self._worker.stop()
            self.statusBar().showMessage("Stopping…")

    def _on_message(self, mail: Email) -> None:
        first = self._model.rowCount() == 0
        self._model.add(mail)
        self.statusBar().showMessage(f"{self._model.rowCount()} message(s) loaded")
        if first:
            index = self._proxy.index(0, 0)
            if index.isValid():
                self._list_view.setCurrentIndex(index)

    def _on_finished(self, count: int, error: str) -> None:
        self._thread, self._worker = None, None
        self._fetch_action.setEnabled(True)
        self._stop_action.setEnabled(False)
        if error:
            self.statusBar().showMessage("Download failed")
            QMessageBox.warning(self, APP_NAME, error)
        elif count == 0:
            self.statusBar().showMessage("No messages matched")
        else:
            self.statusBar().showMessage(f"Done · {count} message(s)")

    # ---------------------------------------------------------------- closing
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self._worker is not None and hasattr(self._worker, "stop"):
            self._worker.stop()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(3000)
        self._settings.fetch_limit = self._limit.value()
        self._settings.default_filter = self._filter.currentData()
        self._settings.save()
        super().closeEvent(event)


def run(settings: Optional[AppSettings] = None, eml_paths: Optional[list[str]] = None) -> int:
    """Create the application and show the window.  Returns the exit code."""
    settings = settings or AppSettings.load()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("EmailChecking")

    window = MainWindow(settings)
    window.show()
    if eml_paths:
        window._start_worker(FileLoadWorker(list(eml_paths)), "Loading files…")
    return app.exec()
