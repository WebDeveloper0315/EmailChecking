"""Compose window: rich text editor, attachments, drag & drop, sending.

The window never talks to SMTP itself - :class:`SendWorker` does that in its
own thread, so a slow server cannot freeze the UI.  A successful send emits
:attr:`ComposeWindow.message_sent` with the raw bytes, which the main window
appends to the Sent folder.

Inline images are handled through Qt's document resources: inserting a picture
registers it under a ``cid:`` URL and writes ``<img src="cid:...">`` into the
document, so the same identifier ends up in the MIME part - no rewriting of the
HTML afterwards.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional, Sequence

import qt_bootstrap

qt_bootstrap.prepare()

from PySide6.QtCore import QMimeData, QObject, Qt, QThread, QUrl, Signal  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QAction,
    QDesktopServices,
    QFont,
    QImage,
    QKeySequence,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextListFormat,
)
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSplitter,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from attachment_manager import human_size  # noqa: E402
from config import AppSettings  # noqa: E402
from html_processor import sanitize_html  # noqa: E402
from logging_setup import get_logger  # noqa: E402
from mail_sender import (  # noqa: E402
    Draft,
    DraftAttachment,
    SendError,
    SendResult,
    SmtpSender,
    build_message,
)
from models import Email  # noqa: E402

logger = get_logger("send", "mail.compose")

__all__ = ["ComposeWindow", "SendWorker"]

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}


class SendWorker(QObject):
    """Runs one SMTP conversation off the UI thread."""

    finished = Signal(object, str)      # SendResult | None, error text

    def __init__(self, settings, draft: Draft) -> None:
        super().__init__()
        self._settings = settings
        self._draft = draft

    def run(self) -> None:
        try:
            result = SmtpSender(self._settings).send(self._draft)
            self.finished.emit(result, "")
        except SendError as exc:
            self.finished.emit(None, str(exc))
        except Exception as exc:  # never lose the thread to a bug
            logger.exception("Unexpected error while sending")
            self.finished.emit(None, f"Unexpected error: {exc}")


class _ComposeEditor(QTextEdit):
    """Rich text editor that hands dropped files to the compose window."""

    files_dropped = Signal(list)        # list[str]

    def canInsertFromMimeData(self, source: QMimeData) -> bool:  # noqa: N802
        if source.hasUrls():
            return True
        return super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source: QMimeData) -> None:  # noqa: N802
        if source.hasUrls():
            paths = [url.toLocalFile() for url in source.urls() if url.isLocalFile()]
            if paths:
                self.files_dropped.emit(paths)
                return
        super().insertFromMimeData(source)


class ComposeWindow(QMainWindow):
    """New message / reply / forward."""

    message_sent = Signal(object)       # SendResult
    draft_saved = Signal(object)        # bytes

    def __init__(
        self,
        settings: AppSettings,
        draft: Optional[Draft] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._attachments: list[DraftAttachment] = []
        self._thread: Optional[QThread] = None
        self._worker: Optional[SendWorker] = None
        self._progress: Optional[QProgressDialog] = None

        self.setWindowTitle("New message")
        self.resize(860, 700)
        self.setAcceptDrops(True)

        self._build_ui()
        if draft is not None:
            self.load_draft(draft)
        else:
            self._from.setText(self._default_from())

    # ------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        form = QFormLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(4)

        self._from = QLineEdit()
        self._from.setReadOnly(True)
        self._to = QLineEdit()
        self._to.setPlaceholderText("recipient@example.com, another@example.com")
        self._cc = QLineEdit()
        self._bcc = QLineEdit()
        self._subject = QLineEdit()

        form.addRow("From:", self._from)
        form.addRow("To:", self._to)
        self._cc_label = QLabel("Cc:")
        self._bcc_label = QLabel("Bcc:")
        form.addRow(self._cc_label, self._cc)
        form.addRow(self._bcc_label, self._bcc)
        form.addRow("Subject:", self._subject)

        options = QHBoxLayout()
        self._show_cc = QCheckBox("Cc / Bcc")
        self._show_cc.toggled.connect(self._toggle_cc)
        self._priority = QCheckBox("High priority")
        self._format = QComboBox()
        self._format.addItems(["Rich text (HTML)", "Plain text"])
        self._format.currentIndexChanged.connect(self._on_format_changed)
        options.addWidget(self._show_cc)
        options.addWidget(self._priority)
        options.addStretch(1)
        options.addWidget(QLabel("Format:"))
        options.addWidget(self._format)
        form.addRow("", options)
        layout.addLayout(form)

        layout.addWidget(self._build_format_toolbar())

        splitter = QSplitter(Qt.Vertical)
        self._editor = _ComposeEditor()
        self._editor.setAcceptRichText(True)
        self._editor.files_dropped.connect(self.add_files)
        self._editor.setFont(QFont("Segoe UI", 10))
        splitter.addWidget(self._editor)

        attachment_box = QWidget()
        attachment_layout = QVBoxLayout(attachment_box)
        attachment_layout.setContentsMargins(0, 4, 0, 0)
        header = QHBoxLayout()
        self._attachment_label = QLabel("Attachments")
        header.addWidget(self._attachment_label)
        header.addStretch(1)
        add_button = QPushButton("Add…")
        add_button.clicked.connect(self.choose_files)
        remove_button = QPushButton("Remove")
        remove_button.clicked.connect(self._remove_selected)
        header.addWidget(add_button)
        header.addWidget(remove_button)
        attachment_layout.addLayout(header)
        self._attachment_list = QListWidget()
        self._attachment_list.setMaximumHeight(120)
        attachment_layout.addWidget(self._attachment_list)
        splitter.addWidget(attachment_box)
        splitter.setSizes([460, 130])
        layout.addWidget(splitter, 1)

        buttons = QHBoxLayout()
        self._send_button = QPushButton("Send")
        self._send_button.setShortcut(QKeySequence("Ctrl+Return"))
        self._send_button.setDefault(True)
        self._send_button.clicked.connect(self.send)
        save_button = QPushButton("Save draft")
        save_button.setShortcut(QKeySequence.Save)
        save_button.clicked.connect(self.save_draft)
        close_button = QPushButton("Discard")
        close_button.clicked.connect(self.close)
        buttons.addWidget(self._send_button)
        buttons.addWidget(save_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        self.setCentralWidget(central)
        self.statusBar().showMessage("Drag files onto the window to attach them")
        self._toggle_cc(False)
        self._update_attachment_label()

    def _build_format_toolbar(self) -> QToolBar:
        toolbar = QToolBar("Formatting")
        toolbar.setMovable(False)

        def add(text: str, tooltip: str, slot, shortcut: str = "", checkable: bool = False):
            action = QAction(text, self)
            action.setToolTip(tooltip)
            action.setCheckable(checkable)
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(slot)
            toolbar.addAction(action)
            return action

        self._bold_action = add("B", "Bold", self._toggle_bold, "Ctrl+B", True)
        font = self._bold_action.font()
        font.setBold(True)
        self._bold_action.setFont(font)
        self._italic_action = add("I", "Italic", self._toggle_italic, "Ctrl+I", True)
        self._underline_action = add("U", "Underline", self._toggle_underline, "Ctrl+U", True)
        toolbar.addSeparator()
        add("• List", "Bulleted list", lambda: self._insert_list(QTextListFormat.ListDisc))
        add("1. List", "Numbered list", lambda: self._insert_list(QTextListFormat.ListDecimal))
        toolbar.addSeparator()
        add("Link", "Insert a hyperlink", self._insert_link)
        add("Image", "Insert an inline image", self.insert_inline_image)
        add("Clear", "Remove formatting", self._clear_format)
        return toolbar

    # ---------------------------------------------------------- formatting
    def _merge_format(self, char_format: QTextCharFormat) -> None:
        cursor = self._editor.textCursor()
        cursor.mergeCharFormat(char_format)
        self._editor.mergeCurrentCharFormat(char_format)

    def _toggle_bold(self) -> None:
        fmt = QTextCharFormat()
        weight = QFont.Normal if self._editor.fontWeight() > QFont.Normal else QFont.Bold
        fmt.setFontWeight(weight)
        self._merge_format(fmt)

    def _toggle_italic(self) -> None:
        fmt = QTextCharFormat()
        fmt.setFontItalic(not self._editor.fontItalic())
        self._merge_format(fmt)

    def _toggle_underline(self) -> None:
        fmt = QTextCharFormat()
        fmt.setFontUnderline(not self._editor.fontUnderline())
        self._merge_format(fmt)

    def _insert_list(self, style) -> None:
        cursor = self._editor.textCursor()
        list_format = QTextListFormat()
        list_format.setStyle(style)
        cursor.createList(list_format)

    def _insert_link(self) -> None:
        cursor = self._editor.textCursor()
        selected = cursor.selectedText()
        from PySide6.QtWidgets import QInputDialog

        url, ok = QInputDialog.getText(self, "Insert link", "URL:", text="https://")
        if not ok or not url:
            return
        fmt = QTextCharFormat()
        fmt.setAnchor(True)
        fmt.setAnchorHref(url)
        fmt.setForeground(Qt.blue)
        fmt.setFontUnderline(True)
        if selected:
            cursor.mergeCharFormat(fmt)
        else:
            cursor.insertText(url, fmt)

    def _clear_format(self) -> None:
        cursor = self._editor.textCursor()
        cursor.setCharFormat(QTextCharFormat())
        self._editor.setCurrentCharFormat(QTextCharFormat())

    def _on_format_changed(self, index: int) -> None:
        plain = index == 1
        if plain:
            text = self._editor.toPlainText()
            self._editor.setPlainText(text)
        self._editor.setAcceptRichText(not plain)

    def _toggle_cc(self, checked: bool) -> None:
        for widget in (self._cc, self._cc_label, self._bcc, self._bcc_label):
            widget.setVisible(checked or bool(self._cc.text() or self._bcc.text()))

    # --------------------------------------------------------- attachments
    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt API
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.add_files(paths)
            event.acceptProposedAction()

    def choose_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Attach files", str(Path.home()))
        if paths:
            self.add_files(paths)

    def add_files(self, paths: Sequence[str]) -> None:
        for path in paths:
            file_path = Path(path)
            if not file_path.is_file():
                continue
            try:
                attachment = DraftAttachment.from_path(file_path)
            except OSError as exc:
                QMessageBox.warning(self, "Attachment",
                                    f"Could not read {file_path.name}:\n{exc}")
                continue
            self._attachments.append(attachment)
        self._refresh_attachments()

    def insert_inline_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Insert image", str(Path.home()),
            "Images (*.png *.jpg *.jpeg *.gif *.bmp *.webp)",
        )
        if not path:
            return
        self._insert_image_path(Path(path))

    def _insert_image_path(self, file_path: Path) -> None:
        try:
            data = file_path.read_bytes()
        except OSError as exc:
            QMessageBox.warning(self, "Image", f"Could not read {file_path.name}:\n{exc}")
            return
        image = QImage()
        if not image.loadFromData(data):
            QMessageBox.warning(self, "Image", f"{file_path.name} is not a readable image.")
            return

        content_id = f"img{uuid.uuid4().hex[:12]}"
        url = QUrl(f"cid:{content_id}")
        self._editor.document().addResource(QTextDocument.ImageResource, url, image)
        cursor = self._editor.textCursor()
        cursor.insertImage(url.toString())

        import mimetypes

        guessed, _ = mimetypes.guess_type(file_path.name)
        self._attachments.append(DraftAttachment(
            filename=file_path.name, data=data,
            content_type=guessed or "image/png", content_id=content_id,
        ))
        self._refresh_attachments()
        if self._format.currentIndex() == 1:
            self._format.setCurrentIndex(0)     # inline images need HTML

    def _remove_selected(self) -> None:
        for item in self._attachment_list.selectedItems():
            index = self._attachment_list.row(item)
            if 0 <= index < len(self._attachments):
                del self._attachments[index]
        self._refresh_attachments()

    def _refresh_attachments(self) -> None:
        self._attachment_list.clear()
        for attachment in self._attachments:
            label = f"{attachment.filename}   {human_size(len(attachment.data))}"
            if attachment.is_inline:
                label += "   (inline)"
            self._attachment_list.addItem(QListWidgetItem(label))
        self._update_attachment_label()

    def _update_attachment_label(self) -> None:
        total = sum(len(a.data) for a in self._attachments)
        if self._attachments:
            self._attachment_label.setText(
                f"Attachments ({len(self._attachments)}, {human_size(total)})"
            )
        else:
            self._attachment_label.setText("Attachments (none)")

    # --------------------------------------------------------------- draft
    def _default_from(self) -> str:
        smtp = self._settings.smtp_settings()
        address = smtp.username or self._settings.account.username
        return f"{smtp.from_name} <{address}>" if smtp.from_name else address

    def load_draft(self, draft: Draft) -> None:
        """Fill the window from a prepared draft (reply / forward / stored)."""
        self._to.setText(", ".join(draft.to))
        self._cc.setText(", ".join(draft.cc))
        self._bcc.setText(", ".join(draft.bcc))
        self._subject.setText(draft.subject)
        self._priority.setChecked(draft.high_priority)
        self._from.setText(
            f"{draft.from_name} <{draft.from_address}>" if draft.from_name
            else draft.from_address or self._default_from()
        )
        if draft.cc or draft.bcc:
            self._show_cc.setChecked(True)

        self._attachments = list(draft.attachments)
        for image in draft.inline_images:
            picture = QImage()
            if picture.loadFromData(image.data):
                self._editor.document().addResource(
                    QTextDocument.ImageResource, QUrl(f"cid:{image.content_id}"), picture
                )
        self._refresh_attachments()

        if draft.body_html:
            self._editor.setHtml(draft.body_html)
        else:
            self._editor.setPlainText(draft.body_text)
        self._draft_headers = draft
        self.setWindowTitle(draft.subject or "New message")

        # Put the cursor above the quoted text, where a reply is written.
        cursor = self._editor.textCursor()
        cursor.movePosition(QTextCursor.Start)
        self._editor.setTextCursor(cursor)

    def current_draft(self) -> Draft:
        """Collect the window's state into a :class:`Draft`."""
        base: Draft = getattr(self, "_draft_headers", Draft())
        plain_only = self._format.currentIndex() == 1

        html = ""
        if not plain_only:
            # Qt writes a whole document with a <head>; the sanitiser reduces it
            # to a body fragment while keeping the inline styles and cid: images.
            html = sanitize_html(
                self._editor.toHtml(),
                cid_resolver=lambda cid: f"cid:{cid}",
                allow_remote_images=True,
            ).html

        from_text = self._from.text().strip()
        from email.utils import parseaddr

        from_name, from_address = parseaddr(from_text)

        return Draft(
            to=_split_addresses(self._to.text()),
            cc=_split_addresses(self._cc.text()),
            bcc=_split_addresses(self._bcc.text()),
            subject=self._subject.text().strip(),
            body_text=self._editor.toPlainText(),
            body_html=html,
            attachments=list(self._attachments),
            from_address=from_address or from_text,
            from_name=from_name,
            reply_to=base.reply_to,
            high_priority=self._priority.isChecked(),
            in_reply_to=base.in_reply_to,
            references=list(base.references),
        )

    # ---------------------------------------------------------------- send
    def send(self) -> None:
        if self._thread is not None:
            return
        draft = self.current_draft()
        problems = draft.validate()
        if problems:
            QMessageBox.warning(self, "Cannot send", "\n".join(problems))
            return

        smtp = self._settings.smtp_settings()
        if not smtp.is_complete:
            QMessageBox.warning(
                self, "Cannot send",
                "Sending is not configured.\n\nOpen Settings → Sending and enter the "
                "SMTP server, user name and password.",
            )
            return

        self._send_button.setEnabled(False)
        self._progress = QProgressDialog("Sending message…", "", 0, 0, self)
        self._progress.setCancelButton(None)
        self._progress.setWindowTitle("Sending")
        self._progress.setWindowModality(Qt.WindowModal)
        self._progress.show()

        self._thread = QThread(self)
        self._worker = SendWorker(smtp, draft)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_sent)
        self._worker.finished.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.start()

    def _on_sent(self, result: Optional[SendResult], error: str) -> None:
        if self._progress is not None:
            self._progress.close()
            self._progress = None
        self._thread = None
        self._worker = None
        self._send_button.setEnabled(True)

        if error:
            QMessageBox.critical(self, "Message not sent", error)
            return

        if result is not None and result.refused:
            refused = ", ".join(result.refused)
            QMessageBox.warning(
                self, "Partly delivered",
                f"The message was sent, but these recipients were refused:\n{refused}",
            )
        self.message_sent.emit(result)
        self.close()

    def save_draft(self) -> None:
        draft = self.current_draft()
        try:
            raw = build_message(draft, include_bcc=True).as_bytes()
        except Exception as exc:
            QMessageBox.warning(self, "Draft", f"Could not build the draft:\n{exc}")
            return
        self.draft_saved.emit(raw)
        self.statusBar().showMessage("Draft saved", 5000)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self._thread is not None:
            event.ignore()
            return
        if self._editor.document().isModified() and self._editor.toPlainText().strip():
            answer = QMessageBox.question(
                self, "Discard message?",
                "This message has not been sent. Discard it?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            )
            if answer == QMessageBox.Cancel:
                event.ignore()
                return
            if answer == QMessageBox.Save:
                self.save_draft()
        super().closeEvent(event)


def _split_addresses(text: str) -> list[str]:
    """Split a header field on commas / semicolons, keeping display names."""
    from email.utils import getaddresses

    entries = [entry for entry in getaddresses([text.replace(";", ",")]) if entry[1] or entry[0]]
    result: list[str] = []
    for name, address in entries:
        if not address and not name:
            continue
        result.append(f"{name} <{address}>" if name and address else (address or name))
    return result
