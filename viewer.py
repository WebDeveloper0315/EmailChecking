"""PySide6 desktop mail client.

Layout
------
+---------------------------------------------------------------------------+
| toolbar: Sync | Compose | Reply | Reply all | Forward | Delete | flags ... |
+------------+--------------------------+---------------------------------- +
| folder     | message list             | header block                       |
| tree with  | (sender, subject,        +----------------------------------- +
| unread     |  preview, date, star)    | body (QtWebEngine / QTextBrowser)  |
| counts     |                          +----------------------------------- +
|            |                          | attachments                        |
+------------+--------------------------+------------------------------------+
| status bar: message | progress | unread count | sync indicator              |
+---------------------------------------------------------------------------+

Threading: this module never performs a network call.  Everything goes through
:class:`mail_sync.SyncController`, which owns a worker thread; results come back
as Qt signals and are applied to the models here, on the UI thread.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional, Sequence

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
    QTimer,
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
    QProgressBar,
    QPushButton,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTextBrowser,
    QToolBar,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
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
from logging_setup import get_logger  # noqa: E402
from mail_parser import find_inline_attachment, parse_email  # noqa: E402
from mail_receiver import FILTERS, EmlFileSource, FolderInfo  # noqa: E402
from mail_sender import Draft, build_forward, build_reply  # noqa: E402
from mail_storage import MailStore  # noqa: E402
from mail_sync import SyncController  # noqa: E402
from models import Attachment, Email, format_addresses  # noqa: E402

logger = get_logger("ui")

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
    logging.getLogger(__name__).info(
        "QtWebEngine unavailable (%s); using QTextBrowser instead", _exc
    )
    WEBENGINE_AVAILABLE = False

EMAIL_ROLE = int(Qt.UserRole) + 1
SEARCH_ROLE = int(Qt.UserRole) + 2

SORT_FIELDS: tuple[tuple[str, str], ...] = (
    ("date", "Date"),
    ("from", "Sender"),
    ("subject", "Subject"),
    ("size", "Size"),
    ("unread", "Read status"),
)

SEARCH_FIELDS: tuple[tuple[str, str], ...] = (
    ("all", "Everything"),
    ("subject", "Subject"),
    ("from", "Sender"),
    ("to", "Recipient"),
    ("date", "Date"),
    ("body", "Body"),
    ("attachment", "Attachment name"),
)


def _sort_value(mail: Email, key: str):
    if key == "from":
        return mail.sender_short.lower()
    if key == "subject":
        return mail.display_subject.lower()
    if key == "size":
        return mail.raw_size
    if key == "unread":
        return (mail.is_read, mail.sort_key)
    return mail.sort_key


# ---------------------------------------------------------------------- model
class MessageListModel(QAbstractListModel):
    """Sorted list of the current folder's messages, updated in place."""

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._emails: list[Email] = []
        self._by_uid: dict[str, int] = {}
        self._sort_key = "date"
        self._descending = True

    # ------------------------------------------------------------ Qt model
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
        if role == Qt.ToolTipRole:
            return (f"{mail.sender}\n{mail.display_subject}\n{mail.display_date}\n"
                    f"{human_size(mail.raw_size)}\n\n{mail.preview(200)}")
        return None

    # -------------------------------------------------------------- content
    def set_sort(self, key: str, descending: bool) -> None:
        if key == self._sort_key and descending == self._descending:
            return
        self._sort_key, self._descending = key, descending
        self.beginResetModel()
        self._resort()
        self.endResetModel()

    def set_messages(self, emails: Sequence[Email]) -> None:
        self.beginResetModel()
        self._emails = list(emails)
        self._resort()
        self.endResetModel()

    def clear(self) -> None:
        self.set_messages([])

    def _resort(self) -> None:
        self._emails.sort(key=lambda m: _sort_value(m, self._sort_key),
                          reverse=self._descending)
        self._reindex()

    def _reindex(self) -> None:
        self._by_uid = {mail.uid: row for row, mail in enumerate(self._emails)}

    def _insert_position(self, mail: Email) -> int:
        value = _sort_value(mail, self._sort_key)
        for row, existing in enumerate(self._emails):
            other = _sort_value(existing, self._sort_key)
            if (value > other) if self._descending else (value < other):
                return row
        return len(self._emails)

    def upsert(self, mail: Email) -> None:
        """Insert a new message or refresh an existing one, keeping the order."""
        row = self._by_uid.get(mail.uid)
        if row is not None and 0 <= row < len(self._emails):
            self._emails[row] = mail
            index = self.index(row, 0)
            self.dataChanged.emit(index, index)
            return
        position = self._insert_position(mail)
        self.beginInsertRows(QModelIndex(), position, position)
        self._emails.insert(position, mail)
        self._reindex()
        self.endInsertRows()

    def refresh_uid(self, uid: str) -> None:
        row = self._by_uid.get(str(uid))
        if row is None:
            return
        index = self.index(row, 0)
        self.dataChanged.emit(index, index)

    def remove_uids(self, uids: Sequence[int]) -> None:
        for uid in uids:
            row = self._by_uid.get(str(uid))
            if row is None:
                continue
            self.beginRemoveRows(QModelIndex(), row, row)
            del self._emails[row]
            self._reindex()
            self.endRemoveRows()

    def email_at(self, row: int) -> Optional[Email]:
        return self._emails[row] if 0 <= row < len(self._emails) else None

    def row_for_uid(self, uid: str) -> int:
        return self._by_uid.get(str(uid), -1)

    @property
    def emails(self) -> list[Email]:
        return list(self._emails)


class SearchProxy(QSortFilterProxyModel):
    """Incremental search over the loaded messages, optionally per field."""

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._query = ""
        self._field = "all"
        self._quick = "all"

    def set_query(self, text: str) -> None:
        self._query = (text or "").strip()
        self.invalidateFilter()

    def set_field(self, field: str) -> None:
        self._field = field or "all"
        if self._query:
            self.invalidateFilter()

    def set_quick_filter(self, quick: str) -> None:
        self._quick = quick or "all"
        self.invalidateFilter()

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:  # noqa: N802
        model = self.sourceModel()
        index = model.index(row, 0, parent)
        mail: Optional[Email] = index.data(EMAIL_ROLE)
        if mail is None:
            return False
        if self._quick == "unread" and mail.is_read:
            return False
        if self._quick == "flagged" and not mail.is_starred:
            return False
        if self._quick == "today" and mail.date is not None:
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc)
            when = mail.date if mail.date.tzinfo else mail.date.replace(tzinfo=timezone.utc)
            if (now - when).days > 0:
                return False
        if not self._query:
            return True
        return mail.matches(self._query, self._field)


class MessageDelegate(QStyledItemDelegate):
    """Three-line row with unread, star and attachment indicators."""

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

        # Unread marker: a coloured bar on the left edge.
        if not mail.is_read:
            marker = QColor("#1a73e8") if not selected else primary
            painter.fillRect(QRect(option.rect.left(), option.rect.top() + 6,
                                   3, option.rect.height() - 12), marker)

        bold = QFont(option.font)
        bold.setBold(not mail.is_read)
        small = QFont(option.font)
        small.setPointSizeF(max(7.0, option.font.pointSizeF() - 1))
        small_metrics = QFontMetrics(small)

        # Line 1: sender (left), star + date (right).
        date_text = mail.display_date
        star_text = "★ " if mail.is_starred else ""
        right_text = star_text + date_text
        right_width = small_metrics.horizontalAdvance(right_text) + 6
        painter.setFont(small)
        painter.setPen(QColor("#f5a623") if mail.is_starred and not selected else secondary)
        painter.drawText(
            QRect(rect.right() - right_width, rect.top(), right_width, line_height),
            int(Qt.AlignRight | Qt.AlignVCenter), right_text,
        )
        painter.setFont(bold)
        painter.setPen(primary)
        sender_rect = QRect(rect.left(), rect.top(), rect.width() - right_width - 6, line_height)
        painter.drawText(
            sender_rect, int(Qt.AlignLeft | Qt.AlignVCenter),
            QFontMetrics(bold).elidedText(mail.sender_short, Qt.ElideRight, sender_rect.width()),
        )

        # Line 2: attachment icon + subject.
        painter.setFont(option.font)
        subject_left = rect.left()
        if mail.has_attachments:
            icon_size = line_height - 4
            icon = QApplication.style().standardIcon(QStyle.SP_FileIcon)
            icon.paint(painter, QRect(rect.left(), rect.top() + line_height + 2,
                                      icon_size, icon_size), Qt.AlignCenter)
            subject_left += icon_size + 4
        subject_rect = QRect(subject_left, rect.top() + line_height,
                             rect.right() - subject_left, line_height)
        painter.drawText(
            subject_rect, int(Qt.AlignLeft | Qt.AlignVCenter),
            metrics.elidedText(mail.display_subject, Qt.ElideRight, subject_rect.width()),
        )

        # Line 3: preview.
        painter.setFont(small)
        painter.setPen(secondary)
        preview_rect = QRect(rect.left(), rect.top() + line_height * 2, rect.width(), line_height)
        painter.drawText(
            preview_rect, int(Qt.AlignLeft | Qt.AlignVCenter),
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
        self._mail = mail
        self._allow_remote = allow_remote_images
        if mail is None:
            self._render(build_document("", dark=_is_dark()))
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
        self._render(build_document(result.html, dark=_is_dark()))
        return result

    def _render(self, document: str) -> None:
        if self._web is not None:
            # setContent avoids setHtml's ~2 MB limit (big inline images).
            self._web.page().setContent(
                QByteArray(document.encode("utf-8")), "text/html;charset=utf-8",
                QUrl("about:blank"),
            )
        elif self._text is not None:
            self._text.set_mail(self._mail)
            self._text.setHtml(document)

    def shutdown(self) -> None:
        """Release the web page before the shared profile goes away.

        Qt prints "Release of profile requested but WebEnginePage still not
        deleted" when a page outlives its profile, which happens at exit unless
        the page is destroyed explicitly.
        """
        if self._web is not None:
            web, self._web = self._web, None
            web.setParent(None)
            web.deleteLater()

    def find(self, text: str) -> None:
        if self._web is not None:
            self._web.page().findText(text)
        elif self._text is not None and text:
            if not self._text.find(text):
                cursor = self._text.textCursor()
                cursor.setPosition(0)
                self._text.setTextCursor(cursor)
                self._text.find(text)


def _open_external(url: QUrl) -> None:
    if url.scheme() in ("http", "https", "mailto", "tel", "ftp", "ftps"):
        QDesktopServices.openUrl(url)
    else:
        logger.info("Refused to open link with scheme %r", url.scheme())


def _dim(label: QLabel, alpha: int = 150) -> None:
    """Grey out a label in a way that works in both light and dark themes."""
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
        title = f"Attachments ({len(attachments)})" if attachments else "Attachments (none)"
        if inline_count:
            title += f" · {inline_count} inline image(s)"
        self._title.setText(title)
        self._save_all_button.setEnabled(bool(attachments))

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
        label.setPixmap(pixmap.scaled(
            min(pixmap.width(), int(screen.width() * 0.8)),
            min(pixmap.height(), int(screen.height() * 0.8)),
            Qt.KeepAspectRatio, Qt.SmoothTransformation,
        ))
        layout.addWidget(label)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _info(self, message: str) -> None:
        window = self.window()
        if isinstance(window, QMainWindow):
            window.statusBar().showMessage(message, 6000)


# ------------------------------------------------------------------- folders
class FolderTree(QTreeWidget):
    """Mailbox tree with unread counts; emits the raw IMAP name on selection."""

    folder_selected = Signal(str)

    ICONS = {"inbox": "📥", "sent": "📤", "drafts": "📝", "trash": "🗑",
             "spam": "⚠", "archive": "📦", "all": "🗂", "other": "📁"}

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setMinimumWidth(180)
        self.setRootIsDecorated(True)
        self._items: dict[str, QTreeWidgetItem] = {}
        self.itemSelectionChanged.connect(self._on_selection)

    def set_folders(self, folders: Sequence[FolderInfo]) -> None:
        current = self.current_folder()
        self.clear()
        self._items.clear()

        # Special folders first, then the rest, nested on the delimiter.
        for folder in folders:
            parent_item: Optional[QTreeWidgetItem] = None
            parts = folder.name.split(folder.delimiter) if folder.delimiter else [folder.name]
            path = ""
            for depth, part in enumerate(parts):
                path = f"{path}{folder.delimiter}{part}" if path else part
                item = self._items.get(path)
                if item is None:
                    item = QTreeWidgetItem([part])
                    if parent_item is None:
                        self.addTopLevelItem(item)
                    else:
                        parent_item.addChild(item)
                    self._items[path] = item
                    item.setData(0, Qt.UserRole, path if depth == len(parts) - 1 else "")
                parent_item = item

            item = self._items[folder.name]
            item.setData(0, Qt.UserRole, folder.name if folder.selectable else "")
            item.setText(0, f"{self.ICONS.get(folder.kind, '📁')}  {folder.display}")
            self._update_count(item, folder.display, folder.kind, folder.unread)
            item.setToolTip(0, f"{folder.name}\n{folder.total} message(s), "
                               f"{folder.unread} unread")

        self.expandAll()
        if current:
            self.select_folder(current)

    def _update_count(self, item: QTreeWidgetItem, display: str, kind: str, unread: int) -> None:
        icon = self.ICONS.get(kind, "📁")
        font = item.font(0)
        font.setBold(unread > 0)
        item.setFont(0, font)
        item.setText(0, f"{icon}  {display}" + (f"  ({unread})" if unread else ""))

    def update_unread(self, folder: str, unread: int, display: str = "", kind: str = "") -> None:
        item = self._items.get(folder)
        if item is None:
            return
        text = display or item.text(0).split("  ", 1)[-1].split("  (")[0]
        self._update_count(item, text, kind or "other", unread)

    def current_folder(self) -> str:
        items = self.selectedItems()
        if not items:
            return ""
        return str(items[0].data(0, Qt.UserRole) or "")

    def select_folder(self, folder: str) -> None:
        item = self._items.get(folder)
        if item is not None:
            self.setCurrentItem(item)

    def _on_selection(self) -> None:
        folder = self.current_folder()
        if folder:
            self.folder_selected.emit(folder)


# ------------------------------------------------------------- file loading
class FileLoadWorker(QObject):
    """Parses ``.eml`` files off the UI thread (kept from the viewer version)."""

    message_ready = Signal(object)
    finished = Signal(int, str)

    def __init__(self, paths: list[str]) -> None:
        super().__init__()
        self._paths = paths

    def run(self) -> None:
        count, error = 0, ""
        try:
            for raw in EmlFileSource.read(self._paths):
                self.message_ready.emit(
                    parse_email(raw.raw, uid=raw.uid, source=raw.folder, folder="(files)")
                )
                count += 1
        except Exception as exc:
            logger.exception("Failed to load .eml files")
            error = str(exc)
        self.finished.emit(count, error)


# ----------------------------------------------------------------- main window
class MainWindow(QMainWindow):
    def __init__(self, settings: AppSettings, start_offline: bool = False) -> None:
        super().__init__()
        self._settings = settings
        #: True when the window was opened on local files: do not ask for an
        #: account and do not start synchronising.
        self._start_offline = start_offline
        self._store = MailStore(
            cache_dir=settings.sync.cache_path() if settings.sync.cache_enabled else None,
            account_key=settings.account_key(),
            cache_enabled=settings.sync.cache_enabled,
            max_messages_per_folder=settings.sync.max_messages_per_folder,
        )
        self._folders: dict[str, FolderInfo] = {}
        self._current_folder = settings.account.folder or "INBOX"
        self._current: Optional[Email] = None
        self._allow_remote_for_current = settings.allow_remote_images
        self._compose_windows: list[QWidget] = []
        self._file_thread: Optional[QThread] = None
        self._file_worker: Optional[QObject] = None
        self._syncing = False

        self.setWindowTitle(APP_NAME)
        self._restore_geometry()

        self._model = MessageListModel(self)
        self._model.set_sort(settings.sort_key, settings.sort_descending)
        self._proxy = SearchProxy(self)
        self._proxy.setSourceModel(self._model)

        self._build_toolbar()
        self._build_central()
        self._build_status_bar()

        self._controller = SyncController(settings.account, self._store, settings, self)
        self._connect_worker()
        if start_offline:
            # Opened on local files: never reach for the server on a timer.
            self._controller.apply_interval(0)

        self.statusBar().showMessage(f"Ready · rendering with {self._body.backend}")
        QTimer.singleShot(0, self._start_up)

    # ---------------------------------------------------------------- set-up
    def _restore_geometry(self) -> None:
        window = self._settings.window
        self.resize(max(800, window.width), max(600, window.height))
        if window.x >= 0 and window.y >= 0:
            self.move(window.x, window.y)
        if window.maximized:
            self.showMaximized()

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.addToolBar(toolbar)

        self._sync_action = QAction("Sync now", self)
        self._sync_action.setShortcut(QKeySequence("F5"))
        self._sync_action.triggered.connect(self.sync_now)
        toolbar.addAction(self._sync_action)

        self._stop_action = QAction("Stop", self)
        self._stop_action.setEnabled(False)
        self._stop_action.triggered.connect(lambda: self._controller.stop_current())
        toolbar.addAction(self._stop_action)
        toolbar.addSeparator()

        compose_action = QAction("Compose", self)
        compose_action.setShortcut(QKeySequence("Ctrl+N"))
        compose_action.triggered.connect(self.compose_new)
        toolbar.addAction(compose_action)

        self._reply_action = QAction("Reply", self)
        self._reply_action.setShortcut(QKeySequence("Ctrl+R"))
        self._reply_action.triggered.connect(lambda: self.reply(all_recipients=False))
        toolbar.addAction(self._reply_action)

        self._reply_all_action = QAction("Reply all", self)
        self._reply_all_action.setShortcut(QKeySequence("Ctrl+Shift+R"))
        self._reply_all_action.triggered.connect(lambda: self.reply(all_recipients=True))
        toolbar.addAction(self._reply_all_action)

        forward_button = QToolButton()
        forward_button.setText("Forward")
        forward_button.setPopupMode(QToolButton.MenuButtonPopup)
        forward_menu = QMenu(forward_button)
        forward_menu.addAction("Forward inline", lambda: self.forward(as_attachment=False))
        forward_menu.addAction("Forward as attachment (.eml)",
                               lambda: self.forward(as_attachment=True))
        forward_button.setMenu(forward_menu)
        forward_button.clicked.connect(lambda: self.forward(as_attachment=False))
        toolbar.addWidget(forward_button)

        self._delete_action = QAction("Delete", self)
        self._delete_action.setShortcut(QKeySequence.Delete)
        self._delete_action.triggered.connect(self.delete_selected)
        toolbar.addAction(self._delete_action)
        toolbar.addSeparator()

        self._read_action = QAction("Mark read", self)
        self._read_action.triggered.connect(lambda: self.set_read(True))
        toolbar.addAction(self._read_action)
        self._unread_action = QAction("Mark unread", self)
        self._unread_action.triggered.connect(lambda: self.set_read(False))
        toolbar.addAction(self._unread_action)
        self._star_action = QAction("Star", self)
        self._star_action.setCheckable(True)
        self._star_action.triggered.connect(self.toggle_star)
        toolbar.addAction(self._star_action)
        toolbar.addSeparator()

        self._images_action = QAction("Show remote images", self)
        self._images_action.setCheckable(True)
        self._images_action.setChecked(self._settings.allow_remote_images)
        self._images_action.toggled.connect(self._toggle_remote_images)
        toolbar.addAction(self._images_action)

        settings_action = QAction("Settings…", self)
        settings_action.triggered.connect(self.open_settings)
        toolbar.addAction(settings_action)

        open_action = QAction("Open .eml…", self)
        open_action.setShortcut(QKeySequence.Open)
        open_action.triggered.connect(self.open_files)
        toolbar.addAction(open_action)

        # ---- second row: view controls
        filters = QToolBar("Filters")
        filters.setMovable(False)
        self.addToolBarBreak()
        self.addToolBar(filters)

        filters.addWidget(QLabel(" Show: "))
        self._filter = QComboBox()
        for value, label in FILTERS:
            self._filter.addItem(label, value)
        index = self._filter.findData(self._settings.default_filter)
        self._filter.setCurrentIndex(max(0, index))
        self._filter.currentIndexChanged.connect(self._on_filter_changed)
        filters.addWidget(self._filter)

        filters.addWidget(QLabel("  Sort: "))
        self._sort = QComboBox()
        for value, label in SORT_FIELDS:
            self._sort.addItem(label, value)
        self._sort.setCurrentIndex(max(0, self._sort.findData(self._settings.sort_key)))
        self._sort.currentIndexChanged.connect(self._on_sort_changed)
        filters.addWidget(self._sort)

        self._sort_direction = QToolButton()
        self._sort_direction.setCheckable(True)
        self._sort_direction.setChecked(self._settings.sort_descending)
        self._sort_direction.setText("▼" if self._settings.sort_descending else "▲")
        self._sort_direction.setToolTip("Sort direction")
        self._sort_direction.toggled.connect(self._on_sort_changed)
        filters.addWidget(self._sort_direction)

        filters.addSeparator()
        filters.addWidget(QLabel(" Search in: "))
        self._search_field = QComboBox()
        for value, label in SEARCH_FIELDS:
            self._search_field.addItem(label, value)
        self._search_field.currentIndexChanged.connect(
            lambda: self._proxy.set_field(self._search_field.currentData())
        )
        filters.addWidget(self._search_field)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search messages…  (Ctrl+F)")
        self._search.setClearButtonEnabled(True)
        self._search.setMinimumWidth(240)
        self._search.textChanged.connect(self._on_search_changed)
        filters.addWidget(self._search)

        server_search = QAction("Search server", self)
        server_search.setToolTip("Ask the server for messages that are not loaded yet")
        server_search.triggered.connect(self.search_on_server)
        filters.addAction(server_search)

        find_shortcut = QAction(self)
        find_shortcut.setShortcut(QKeySequence.Find)
        find_shortcut.triggered.connect(self._search.setFocus)
        self.addAction(find_shortcut)

    def _build_central(self) -> None:
        self._splitter = QSplitter(Qt.Horizontal)

        self._folder_tree = FolderTree()
        self._folder_tree.folder_selected.connect(self.open_folder)
        self._splitter.addWidget(self._folder_tree)

        self._list_view = QListView()
        self._list_view.setModel(self._proxy)
        self._list_view.setItemDelegate(MessageDelegate(self))
        self._list_view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._list_view.setUniformItemSizes(True)
        self._list_view.setMinimumWidth(260)
        self._list_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self._list_view.customContextMenuRequested.connect(self._list_context_menu)
        self._list_view.selectionModel().currentChanged.connect(self._on_selection)
        self._splitter.addWidget(self._list_view)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self._header = HeaderPane()
        right_layout.addWidget(self._header)

        self._banner = QLabel()
        self._banner.setWordWrap(True)
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

        self._splitter.addWidget(right)
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 0)
        self._splitter.setStretchFactor(2, 1)
        sizes = self._settings.window.sizes()
        self._splitter.setSizes(sizes if len(sizes) == 3 else [200, 330, 720])
        self.setCentralWidget(self._splitter)

    def _build_status_bar(self) -> None:
        bar = self.statusBar()
        self._progress = QProgressBar()
        self._progress.setMaximumWidth(160)
        self._progress.setMaximumHeight(14)
        self._progress.hide()
        self._unread_label = QLabel("")
        self._sync_label = QLabel("Idle")
        _dim(self._sync_label)
        bar.addPermanentWidget(self._progress)
        bar.addPermanentWidget(self._unread_label)
        bar.addPermanentWidget(self._sync_label)

    def _connect_worker(self) -> None:
        worker = self._controller.worker
        worker.folders_listed.connect(self._on_folders)
        worker.message_arrived.connect(self._on_message_arrived)
        worker.flags_changed.connect(self._on_flags_changed)
        worker.messages_removed.connect(self._on_messages_removed)
        worker.sync_started.connect(self._on_sync_started)
        worker.sync_progress.connect(self._on_sync_progress)
        worker.sync_finished.connect(self._on_sync_finished)
        worker.operation_finished.connect(self._on_operation_finished)
        worker.connection_changed.connect(self._on_connection_changed)
        worker.cache_loaded.connect(
            lambda folder, count: self.statusBar().showMessage(
                f"{count} message(s) restored from the local cache", 5000) if count else None
        )

    def _start_up(self) -> None:
        if self._start_offline:
            self.statusBar().showMessage("Opened local files · not connected", 8000)
            return
        account = self._settings.account
        if not account.username or not account.password:
            self.statusBar().showMessage("No account configured - open Settings…")
            self.open_settings()
            account = self._settings.account
            if not account.username or not account.password:
                return
        self._controller.set_target(self._current_folder, self._filter.currentData())
        self._controller.list_folders()
        self._controller.load_cache(self._current_folder,
                                    self._settings.sync.max_messages_per_folder)
        if self._settings.sync.sync_on_start:
            self._controller.sync_now(self._current_folder, self._filter.currentData())

    # -------------------------------------------------------------- folders
    def _on_folders(self, folders: Sequence[FolderInfo]) -> None:
        self._folders = {folder.name: folder for folder in folders}
        self._folder_tree.set_folders(folders)
        if self._current_folder not in self._folders and folders:
            inbox = next((f for f in folders if f.kind == "inbox"), folders[0])
            self._current_folder = inbox.name
        self._folder_tree.select_folder(self._current_folder)
        self._update_unread_label()

    def open_folder(self, folder: str) -> None:
        if not folder or folder == self._current_folder:
            return
        logger.info("Opening folder %s", folder, extra={"event": "open_folder", "folder": folder})
        self._current_folder = folder
        self._current = None
        self._model.set_messages(self._store.messages(folder))
        self._render_current()
        self._controller.set_target(folder, self._filter.currentData())
        self._controller.load_cache(folder, self._settings.sync.max_messages_per_folder)
        self._controller.sync_now(folder, self._filter.currentData())
        self._select_first_row()

    def _folder_of_kind(self, kind: str) -> Optional[str]:
        for folder in self._folders.values():
            if folder.kind == kind:
                return folder.name
        return None

    # ------------------------------------------------------------- syncing
    def sync_now(self) -> None:
        self._controller.sync_now(self._current_folder, self._filter.currentData())

    def _on_sync_started(self, folder: str) -> None:
        self._syncing = True
        self._sync_label.setText(f"⟳ Syncing {folder}…")
        self._stop_action.setEnabled(True)
        self._progress.setRange(0, 0)
        self._progress.show()

    def _on_sync_progress(self, done: int, total: int) -> None:
        if total > 0:
            self._progress.setRange(0, total)
            self._progress.setValue(done)
        self.statusBar().showMessage(f"Downloading {done}/{total}…")

    def _on_sync_finished(self, result) -> None:
        self._syncing = False
        self._stop_action.setEnabled(False)
        self._progress.hide()
        from datetime import datetime

        stamp = datetime.now().strftime("%H:%M:%S")
        if result.error:
            self._sync_label.setText(f"⚠ Sync failed at {stamp}")
            self.statusBar().showMessage(result.error, 15000)
        else:
            self._sync_label.setText(f"Last sync {stamp}")
            summary = []
            if result.new_messages:
                summary.append(f"{len(result.new_messages)} new")
            if result.flag_updates:
                summary.append(f"{len(result.flag_updates)} updated")
            if result.removed_uids:
                summary.append(f"{len(result.removed_uids)} removed")
            self.statusBar().showMessage(
                f"Sync of {result.folder}: " + (", ".join(summary) or "no changes"), 8000
            )
            folder = self._folders.get(result.folder)
            if folder is not None:
                folder.unread = result.unread
                folder.total = result.total
                self._folder_tree.update_unread(result.folder, result.unread,
                                                folder.display, folder.kind)
        self._update_unread_label()

    def _on_message_arrived(self, mail: Email) -> None:
        if mail.folder != self._current_folder and mail.folder != "(files)":
            return
        scrollbar = self._list_view.verticalScrollBar()
        at_top = scrollbar.value() == scrollbar.minimum()
        offset = scrollbar.value()
        selected = self._current.uid if self._current else None

        self._model.upsert(mail)

        # Keep the viewport where the user left it unless they are at the top,
        # where new mail should simply appear.
        if not at_top:
            scrollbar.setValue(offset)
        if selected:
            self._reselect(selected)
        elif self._current is None and self._model.rowCount() == 1:
            self._select_first_row()
        self._update_unread_label()

    def _on_flags_changed(self, folder: str, uid: int, flags) -> None:
        if folder != self._current_folder:
            return
        self._model.refresh_uid(str(uid))
        if self._current is not None and self._current.uid == str(uid):
            self._star_action.setChecked(self._current.is_starred)
        self._update_unread_label()

    def _on_messages_removed(self, folder: str, uids: Sequence[int]) -> None:
        if folder != self._current_folder:
            return
        removing_current = self._current is not None and self._current.uid_number in set(uids)
        self._model.remove_uids(list(uids))
        if removing_current:
            self._current = None
            self._select_first_row()
        self._update_unread_label()

    def _on_operation_finished(self, operation: str, ok: bool, message: str) -> None:
        if message:
            self.statusBar().showMessage(message, 8000)
        if not ok:
            logger.warning("Operation %s failed: %s", operation, message)
            if operation in ("delete", "flags", "move", "append", "search"):
                QMessageBox.warning(self, APP_NAME, f"{operation.capitalize()} failed:\n{message}")

    def _on_connection_changed(self, connected: bool, message: str) -> None:
        self._sync_label.setText("Connected" if connected else "Disconnected")
        if not connected and message:
            self.statusBar().showMessage(message, 15000)

    def _update_unread_label(self) -> None:
        total, unread = self._store.counts(self._current_folder)
        shown = self._proxy.rowCount()
        self._unread_label.setText(f"{shown}/{total} shown · {unread} unread")

    # ------------------------------------------------------------ selection
    def _select_first_row(self) -> None:
        if self._proxy.rowCount() > 0:
            self._list_view.setCurrentIndex(self._proxy.index(0, 0))

    def _reselect(self, uid: str) -> None:
        row = self._model.row_for_uid(uid)
        if row < 0:
            return
        proxy_index = self._proxy.mapFromSource(self._model.index(row, 0))
        if proxy_index.isValid() and self._list_view.currentIndex() != proxy_index:
            self._list_view.selectionModel().setCurrentIndex(
                proxy_index, self._list_view.selectionModel().SelectionFlag.NoUpdate
            )

    def _selected_messages(self) -> list[Email]:
        messages: list[Email] = []
        for index in self._list_view.selectionModel().selectedIndexes():
            mail = index.data(EMAIL_ROLE)
            if mail is not None and mail not in messages:
                messages.append(mail)
        if not messages and self._current is not None:
            messages.append(self._current)
        return messages

    def _on_selection(self, current: QModelIndex, _previous: QModelIndex) -> None:
        mail: Optional[Email] = current.data(EMAIL_ROLE) if current.isValid() else None
        self._current = mail
        self._allow_remote_for_current = self._settings.allow_remote_images
        self._images_action.setChecked(self._allow_remote_for_current)
        self._render_current()

        if mail is not None:
            self._star_action.setChecked(mail.is_starred)
            if self._settings.mark_seen and not mail.is_read and mail.uid_number:
                self.set_read(True)

    def _render_current(self) -> None:
        mail = self._current
        has_message = mail is not None
        for action in (self._reply_action, self._reply_all_action, self._delete_action,
                       self._read_action, self._unread_action, self._star_action):
            action.setEnabled(has_message)

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
            notes.append(f"{result.remote_images_blocked} remote image(s) blocked to protect "
                         'your privacy. <a href="#show">Show images</a>')
        if result.trackers_removed:
            notes.append(f"{result.trackers_removed} tracking pixel(s) removed")
        if result.missing_inline_images:
            notes.append(f"{len(result.missing_inline_images)} inline image(s) not found")
        if notes:
            self._banner.setText(" · ".join(notes))
            self._banner.show()
        else:
            self._banner.hide()

    def _toggle_remote_images(self, checked: bool) -> None:
        self._allow_remote_for_current = checked
        if self._current is not None:
            self._render_current()

    # -------------------------------------------------------------- actions
    def _list_context_menu(self, position) -> None:
        if not self._selected_messages():
            return
        menu = QMenu(self)
        menu.addAction("Reply", lambda: self.reply(False))
        menu.addAction("Reply all", lambda: self.reply(True))
        menu.addAction("Forward", lambda: self.forward(False))
        menu.addSeparator()
        menu.addAction("Mark as read", lambda: self.set_read(True))
        menu.addAction("Mark as unread", lambda: self.set_read(False))
        menu.addAction("Toggle star", self.toggle_star)
        menu.addSeparator()
        move_menu = menu.addMenu("Move to")
        for folder in self._folders.values():
            if folder.selectable and folder.name != self._current_folder:
                move_menu.addAction(folder.display,
                                    lambda checked=False, name=folder.name: self.move_to(name))
        menu.addAction("Delete", self.delete_selected)
        menu.exec(self._list_view.mapToGlobal(position))

    def set_read(self, read: bool) -> None:
        messages = [m for m in self._selected_messages() if m.uid_number]
        if not messages:
            return
        uids = [m.uid_number for m in messages]
        self._controller.set_flags(self._current_folder, uids, ["\\Seen"], add=read)

    def toggle_star(self) -> None:
        messages = [m for m in self._selected_messages() if m.uid_number]
        if not messages:
            return
        add = not all(m.is_starred for m in messages)
        self._controller.set_flags(self._current_folder,
                                   [m.uid_number for m in messages], ["\\Flagged"], add=add)

    def move_to(self, destination: str) -> None:
        messages = [m for m in self._selected_messages() if m.uid_number]
        if not messages:
            return
        self._controller.move(self._current_folder, [m.uid_number for m in messages], destination)

    def delete_selected(self) -> None:
        messages = [m for m in self._selected_messages() if m.uid_number]
        if not messages:
            return
        trash = self._folder_of_kind("trash")
        in_trash = self._current_folder == trash

        box = QMessageBox(self)
        box.setWindowTitle("Delete messages")
        box.setIcon(QMessageBox.Question)
        count = len(messages)
        subject = messages[0].display_subject if count == 1 else f"{count} messages"
        if trash and not in_trash:
            box.setText(f"Delete {subject}?")
            box.setInformativeText(f"They can be moved to “{trash}” or removed for good.")
            move_button = box.addButton("Move to Trash", QMessageBox.AcceptRole)
            permanent_button = box.addButton("Delete permanently", QMessageBox.DestructiveRole)
        else:
            box.setText(f"Permanently delete {subject}?")
            box.setInformativeText("This cannot be undone.")
            move_button = None
            permanent_button = box.addButton("Delete permanently", QMessageBox.DestructiveRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()

        clicked = box.clickedButton()
        if clicked is None or clicked == box.button(QMessageBox.Cancel):
            return
        permanent = clicked == permanent_button

        uids = [m.uid_number for m in messages]
        # Update the UI at once; the worker confirms (or reports a failure).
        self._model.remove_uids(uids)
        if self._current is not None and self._current.uid_number in set(uids):
            self._current = None
            self._render_current()
        self._controller.delete(self._current_folder, uids, permanent=permanent)
        logger.info("Delete requested for %d message(s)", len(uids),
                    extra={"event": "delete_request", "folder": self._current_folder,
                           "count": len(uids), "permanent": permanent})

    # --------------------------------------------------------------- compose
    def _identity(self) -> tuple[str, str]:
        smtp = self._settings.smtp_settings()
        return smtp.username or self._settings.account.username, smtp.from_name

    def _open_compose(self, draft: Optional[Draft]) -> None:
        from compose_window import ComposeWindow

        window = ComposeWindow(self._settings, draft, self)
        window.message_sent.connect(self._on_message_sent)
        window.draft_saved.connect(self._on_draft_saved)
        window.setAttribute(Qt.WA_DeleteOnClose, True)
        window.destroyed.connect(lambda: self._compose_windows.remove(window)
                                 if window in self._compose_windows else None)
        self._compose_windows.append(window)
        window.show()

    def compose_new(self) -> None:
        address, name = self._identity()
        self._open_compose(Draft(from_address=address, from_name=name))

    def reply(self, all_recipients: bool = False) -> None:
        if self._current is None:
            return
        address, name = self._identity()
        draft = build_reply(self._current, address, name, reply_all=all_recipients)
        self._open_compose(draft)

    def forward(self, as_attachment: bool = False) -> None:
        if self._current is None:
            return
        address, name = self._identity()
        raw = None
        if as_attachment:
            raw = self._store.cached_raw(self._current_folder, self._current.uid_number)
            if raw is None:
                self.statusBar().showMessage(
                    "The original bytes are not cached; forwarding a rebuilt copy.", 8000
                )
        draft = build_forward(self._current, address, name,
                              as_attachment=as_attachment, raw_message=raw)
        self._open_compose(draft)

    def _on_message_sent(self, result) -> None:
        self.statusBar().showMessage(
            f"Message sent to {len(result.recipients)} recipient(s)", 8000
        )
        sent_folder = self._folder_of_kind("sent")
        if sent_folder and result is not None:
            self._controller.append(sent_folder, result.raw, ["\\Seen"])

    def _on_draft_saved(self, raw: bytes) -> None:
        drafts = self._folder_of_kind("drafts")
        if not drafts:
            QMessageBox.information(self, APP_NAME,
                                    "No Drafts folder was found on the server.")
            return
        self._controller.append(drafts, raw, ["\\Draft"])

    # ---------------------------------------------------------------- search
    def _on_search_changed(self, text: str) -> None:
        self._proxy.set_query(text)
        self._update_unread_label()

    def search_on_server(self) -> None:
        query = self._search.text().strip()
        if not query:
            self.statusBar().showMessage("Type something to search for first", 5000)
            return
        self.statusBar().showMessage(f"Searching the server for “{query}”…")
        self._controller.search_server(self._current_folder, query)

    def _on_filter_changed(self) -> None:
        criteria = self._filter.currentData()
        self._proxy.set_quick_filter(criteria)
        self._settings.default_filter = criteria
        self._controller.set_target(self._current_folder, criteria)
        self._update_unread_label()

    def _on_sort_changed(self) -> None:
        descending = self._sort_direction.isChecked()
        self._sort_direction.setText("▼" if descending else "▲")
        key = self._sort.currentData()
        self._settings.sort_key = key
        self._settings.sort_descending = descending
        selected = self._current.uid if self._current else None
        self._model.set_sort(key, descending)
        if selected:
            self._reselect(selected)

    # ----------------------------------------------------------- misc slots
    def open_settings(self) -> None:
        from settings_dialog import SettingsDialog

        previous_interval = self._settings.sync.interval_seconds
        previous_account = (self._settings.account.host, self._settings.account.username,
                            self._settings.account.password)
        dialog = SettingsDialog(self._settings, self)
        if dialog.exec() != QDialog.Accepted:
            return

        self._controller.apply_interval(self._settings.sync.interval_seconds)
        current_account = (self._settings.account.host, self._settings.account.username,
                           self._settings.account.password)
        if current_account != previous_account:
            self._store.clear()
            self._model.clear()
            self._controller.update_account(self._settings.account)
            self._controller.list_folders()
            self._controller.sync_now(self._current_folder, self._filter.currentData())
        elif previous_interval != self._settings.sync.interval_seconds:
            self.statusBar().showMessage(
                "Refresh interval updated", 5000
            )
        self._images_action.setChecked(self._settings.allow_remote_images)

    def open_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Open e-mail files", str(Path.home()),
            "E-mail files (*.eml *.msg *.txt);;All files (*)",
        )
        if not paths:
            return
        self._current_folder = "(files)"
        self._model.clear()
        self._start_file_worker(FileLoadWorker(paths), f"Loading {len(paths)} file(s)…")

    def _start_file_worker(self, worker: QObject, message: str) -> None:
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.message_ready.connect(self._on_message_arrived)
        worker.finished.connect(self._on_files_loaded)
        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._file_thread, self._file_worker = thread, worker
        self.statusBar().showMessage(message)
        thread.start()

    def _on_files_loaded(self, count: int, error: str) -> None:
        self._file_thread, self._file_worker = None, None
        if error:
            QMessageBox.warning(self, APP_NAME, error)
        else:
            self.statusBar().showMessage(f"Loaded {count} message(s)")
            self._select_first_row()

    # ---------------------------------------------------------------- close
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        window = self._settings.window
        window.maximized = self.isMaximized()
        if not self.isMaximized():
            window.width, window.height = self.width(), self.height()
            window.x, window.y = self.x(), self.y()
        window.splitter_sizes = ",".join(str(size) for size in self._splitter.sizes())
        self._settings.save()

        for compose in list(self._compose_windows):
            compose.close()
        self._controller.shutdown()
        if self._file_thread is not None:
            self._file_thread.quit()
            self._file_thread.wait(2000)
        self._body.shutdown()
        super().closeEvent(event)


def apply_theme(app: QApplication, theme: str) -> None:
    """Force a light/dark palette, or leave the system default alone."""
    if theme == "dark":
        from PySide6.QtGui import QPalette as _Palette

        palette = _Palette()
        palette.setColor(_Palette.Window, QColor(32, 33, 36))
        palette.setColor(_Palette.WindowText, QColor(232, 234, 237))
        palette.setColor(_Palette.Base, QColor(24, 25, 28))
        palette.setColor(_Palette.AlternateBase, QColor(40, 41, 45))
        palette.setColor(_Palette.Text, QColor(232, 234, 237))
        palette.setColor(_Palette.Button, QColor(45, 46, 50))
        palette.setColor(_Palette.ButtonText, QColor(232, 234, 237))
        palette.setColor(_Palette.Highlight, QColor(26, 115, 232))
        palette.setColor(_Palette.HighlightedText, QColor(255, 255, 255))
        app.setPalette(palette)
    elif theme == "light":
        app.setStyle(app.style().objectName())
        app.setPalette(app.style().standardPalette())


def run(settings: Optional[AppSettings] = None, eml_paths: Optional[list[str]] = None) -> int:
    """Create the application and show the window.  Returns the exit code."""
    settings = settings or AppSettings.load()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("EmailChecking")
    apply_theme(app, settings.theme)

    window = MainWindow(settings, start_offline=bool(eml_paths))
    window.show()
    if eml_paths:
        window._current_folder = "(files)"
        window._start_file_worker(FileLoadWorker(list(eml_paths)), "Loading files…")
    return app.exec()
