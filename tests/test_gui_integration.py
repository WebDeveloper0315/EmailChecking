"""End-to-end GUI tests: the real window, the real sync thread, a fake server.

These drive :class:`viewer.MainWindow` exactly as a user would - selecting
folders, starring, deleting, replying - and then assert on the *server* state,
so they prove the whole chain works: UI -> SyncController -> worker thread ->
ImapClient -> imbox -> IMAP.

A display is required (they open real windows briefly).

    python tests/test_gui_integration.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qt_bootstrap  # noqa: E402

if not qt_bootstrap.prepare():  # pragma: no cover - no GUI stack installed
    print("PySide6 is not available; skipping the GUI tests.")
    raise SystemExit(0)

from PySide6.QtWidgets import QApplication  # noqa: E402

from config import AccountSettings, AppSettings, SmtpSettings  # noqa: E402
from fake_imap import FakeMessage, fake_imap_server, sample_message  # noqa: E402
from fake_smtp import fake_smtp_server  # noqa: E402

import viewer  # noqa: E402

APP = QApplication.instance() or QApplication([])


def pump(condition=None, timeout: float = 8.0) -> bool:
    """Run the event loop until ``condition`` is true or the timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if condition is not None and condition():
            APP.processEvents()
            return True
        time.sleep(0.01)
    APP.processEvents()
    return condition is None or bool(condition())


def inbox(count: int = 4) -> list[FakeMessage]:
    return [
        FakeMessage(
            uid=uid,
            raw=sample_message(subject=f"Message {uid}", body=f"body of message {uid}",
                               sender=f"sender{uid}@example.com"),
            flags=set(),
        )
        for uid in range(1, count + 1)
    ]


def mailboxes(count: int = 4) -> dict[str, list[FakeMessage]]:
    return {"INBOX": inbox(count), "Sent": [], "Trash": [], "Drafts": []}


FOLDER_FLAGS = {"Sent": "\\HasNoChildren \\Sent", "Trash": "\\HasNoChildren \\Trash",
                "Drafts": "\\HasNoChildren \\Drafts"}


class GuiTestCase(unittest.TestCase):
    """Creates a window wired to a fake server, and tears it down cleanly."""

    message_count = 4

    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="mailgui-test-"))
        self.settings = AppSettings(
            account=AccountSettings(host="imap.example.com", port=993,
                                    username="user@example.com", password="secret"),
            smtp=SmtpSettings(host="smtp.example.com", port=587, security="starttls",
                              username="user@example.com", password="secret"),
            path=self.temp / "config.ini",
        )
        self.settings.download_dir = str(self.temp / "downloads")
        self.settings.sync.cache_dir = str(self.temp / "cache")
        self.settings.sync.interval_seconds = 0        # manual: tests drive the sync
        self.settings.sync.sync_on_start = True

        self._server_context = fake_imap_server(mailboxes=mailboxes(self.message_count),
                                                folder_flags=FOLDER_FLAGS)
        self.server = self._server_context.__enter__()

        self.window = viewer.MainWindow(self.settings)
        self.window.show()
        pump(lambda: self.window._model.rowCount() >= self.message_count)

    def tearDown(self) -> None:
        self.window.close()
        pump(timeout=1.0)
        self.window.deleteLater()
        pump(timeout=0.5)
        self._server_context.__exit__(None, None, None)
        shutil.rmtree(self.temp, ignore_errors=True)

    # ------------------------------------------------------------- helpers
    def select_row(self, row: int = 0) -> None:
        index = self.window._proxy.index(row, 0)
        self.window._list_view.setCurrentIndex(index)
        pump(lambda: self.window._current is not None, timeout=3)

    def server_message(self, uid: int, folder: str = "INBOX"):
        for message in self.server.mailboxes[folder]:
            if message.uid == uid:
                return message
        return None


class StartupTests(GuiTestCase):
    def test_messages_and_folders_are_loaded(self) -> None:
        self.assertEqual(self.window._model.rowCount(), self.message_count)
        self.assertIn("INBOX", self.window._folders)
        self.assertEqual(self.window._folders["Sent"].kind, "sent")
        self.assertEqual(self.window._folders["Trash"].kind, "trash")

    def test_newest_message_is_selected_and_rendered(self) -> None:
        self.select_row(0)
        self.assertIsNotNone(self.window._current)
        self.assertEqual(self.window._current.subject, "Message 4")

    def test_unread_count_is_shown(self) -> None:
        self.assertIn("unread", self.window._unread_label.text())
        self.assertIn(str(self.message_count), self.window._unread_label.text())

    def test_no_message_is_marked_read_just_by_syncing(self) -> None:
        self.assertTrue(all("\\Seen" not in m.flags for m in self.server.mailboxes["INBOX"]))


class SyncTests(GuiTestCase):
    def test_new_mail_appears_without_duplicates(self) -> None:
        before = self.window._model.rowCount()
        self.server.mailboxes["INBOX"].append(
            FakeMessage(uid=77, raw=sample_message(subject="Just arrived"))
        )
        self.window.sync_now()
        pump(lambda: self.window._model.rowCount() == before + 1)
        self.assertEqual(self.window._model.rowCount(), before + 1)

        # Syncing again must not add the same message a second time.
        self.window.sync_now()
        pump(lambda: not self.window._syncing, timeout=5)
        self.assertEqual(self.window._model.rowCount(), before + 1)
        subjects = [m.subject for m in self.window._model.emails]
        self.assertEqual(subjects.count("Just arrived"), 1)

    def test_selection_survives_a_sync(self) -> None:
        self.select_row(1)
        selected_uid = self.window._current.uid
        self.server.mailboxes["INBOX"].append(
            FakeMessage(uid=90, raw=sample_message(subject="Another one"))
        )
        self.window.sync_now()
        pump(lambda: self.window._model.rowCount() == self.message_count + 1)
        self.assertEqual(self.window._current.uid, selected_uid,
                         "the open message must stay open while new mail arrives")

    def test_flag_change_on_the_server_is_picked_up(self) -> None:
        self.server.mailboxes["INBOX"][0].flags.add("\\Seen")
        self.window.sync_now()
        pump(lambda: self.window._store.message("INBOX", 1) is not None
             and self.window._store.message("INBOX", 1).is_read)
        self.assertTrue(self.window._store.message("INBOX", 1).is_read)

    def test_message_deleted_elsewhere_disappears(self) -> None:
        self.server.mailboxes["INBOX"] = [
            m for m in self.server.mailboxes["INBOX"] if m.uid != 2
        ]
        self.window.sync_now()
        pump(lambda: self.window._model.row_for_uid("2") < 0)
        self.assertLess(self.window._model.row_for_uid("2"), 0)

    def test_ui_stays_responsive_during_sync(self) -> None:
        """The event loop must keep turning while the worker is downloading.

        The fake server is given a per-message delay, so the download takes
        about a second of wall time.  If that work ran on the UI thread the
        timer below could not fire; off the UI thread it fires continuously.
        """
        self.server.fetch_delay = 0.05          # 50 ms per message body
        self.server.mailboxes["INBOX"].extend(
            FakeMessage(uid=100 + i, raw=sample_message(subject=f"Bulk {i}"))
            for i in range(20)
        )
        ticks = {"count": 0}
        from PySide6.QtCore import QTimer

        timer = QTimer()
        timer.timeout.connect(lambda: ticks.__setitem__("count", ticks["count"] + 1))
        timer.start(10)
        start = time.monotonic()
        self.window.sync_now()
        pump(lambda: self.window._model.rowCount() >= self.message_count + 20, timeout=30)
        elapsed = time.monotonic() - start
        timer.stop()
        self.server.fetch_delay = 0.0

        self.assertGreater(elapsed, 0.8, "the fake latency did not take effect")
        self.assertGreater(ticks["count"], 20,
                           "the UI thread was blocked while messages were downloading")


class MessageActionTests(GuiTestCase):
    def test_star_and_unstar(self) -> None:
        self.select_row(0)
        uid = self.window._current.uid_number
        self.window.toggle_star()
        pump(lambda: "\\Flagged" in self.server_message(uid).flags)
        self.assertIn("\\Flagged", self.server_message(uid).flags)

        self.window.toggle_star()
        pump(lambda: "\\Flagged" not in self.server_message(uid).flags)
        self.assertNotIn("\\Flagged", self.server_message(uid).flags)

    def test_mark_read_and_unread(self) -> None:
        self.select_row(0)
        uid = self.window._current.uid_number
        self.window.set_read(True)
        pump(lambda: "\\Seen" in self.server_message(uid).flags)
        self.assertIn("\\Seen", self.server_message(uid).flags)
        self.assertTrue(self.window._store.message("INBOX", uid).is_read)

        self.window.set_read(False)
        pump(lambda: "\\Seen" not in self.server_message(uid).flags)
        self.assertNotIn("\\Seen", self.server_message(uid).flags)

    def test_move_to_another_folder(self) -> None:
        self.select_row(0)
        uid = self.window._current.uid_number
        self.window.move_to("Trash")
        pump(lambda: len(self.server.mailboxes["Trash"]) == 1)
        self.assertEqual(len(self.server.mailboxes["Trash"]), 1)
        self.assertIsNone(self.server_message(uid))

    def test_delete_moves_to_trash(self) -> None:
        self.select_row(0)
        uid = self.window._current.uid_number
        # Answer the confirmation dialog automatically.
        self._auto_answer_delete(permanent=False)
        self.window.delete_selected()
        pump(lambda: len(self.server.mailboxes["Trash"]) == 1)
        self.assertEqual(len(self.server.mailboxes["Trash"]), 1)
        self.assertIsNone(self.server_message(uid))
        self.assertLess(self.window._model.row_for_uid(str(uid)), 0,
                        "the row must disappear from the list immediately")

    def test_permanent_delete(self) -> None:
        self.select_row(0)
        uid = self.window._current.uid_number
        self._auto_answer_delete(permanent=True)
        self.window.delete_selected()
        pump(lambda: self.server_message(uid) is None)
        self.assertIsNone(self.server_message(uid))
        self.assertEqual(self.server.mailboxes["Trash"], [])

    def _auto_answer_delete(self, permanent: bool) -> None:
        """Patch QMessageBox.exec so the confirmation answers itself."""
        from PySide6.QtWidgets import QMessageBox

        original_exec = QMessageBox.exec

        def fake_exec(box_self):
            buttons = box_self.buttons()
            wanted = "Delete permanently" if permanent else "Move to Trash"
            for button in buttons:
                if button.text().replace("&", "") == wanted:
                    box_self.setResult(0)
                    # clickedButton() reads the internally clicked button
                    box_self.buttonClicked.emit(button)
                    box_self.done(0)
                    box_self._clicked = button
                    return 0
            return 0

        QMessageBox.exec = fake_exec
        original_clicked = QMessageBox.clickedButton
        QMessageBox.clickedButton = lambda box_self: getattr(box_self, "_clicked", None)
        self.addCleanup(lambda: setattr(QMessageBox, "exec", original_exec))
        self.addCleanup(lambda: setattr(QMessageBox, "clickedButton", original_clicked))


class SearchSortFilterTests(GuiTestCase):
    def test_incremental_search_narrows_the_list(self) -> None:
        self.window._search.setText("Message 2")
        pump(lambda: self.window._proxy.rowCount() == 1, timeout=3)
        self.assertEqual(self.window._proxy.rowCount(), 1)
        self.window._search.setText("")
        pump(lambda: self.window._proxy.rowCount() == self.message_count, timeout=3)

    def test_search_by_field(self) -> None:
        self.window._search_field.setCurrentIndex(
            [f[0] for f in viewer.SEARCH_FIELDS].index("from")
        )
        self.window._search.setText("sender3@example.com")
        pump(lambda: self.window._proxy.rowCount() == 1, timeout=3)
        self.assertEqual(self.window._proxy.rowCount(), 1)

        # The same text is not in any subject, so a subject search finds nothing.
        self.window._search_field.setCurrentIndex(
            [f[0] for f in viewer.SEARCH_FIELDS].index("subject")
        )
        pump(lambda: self.window._proxy.rowCount() == 0, timeout=3)
        self.assertEqual(self.window._proxy.rowCount(), 0)

    def test_search_by_body(self) -> None:
        self.window._search_field.setCurrentIndex(
            [f[0] for f in viewer.SEARCH_FIELDS].index("body")
        )
        self.window._search.setText("body of message 1")
        pump(lambda: self.window._proxy.rowCount() == 1, timeout=3)
        self.assertEqual(self.window._proxy.rowCount(), 1)

    def test_quick_filter_unread(self) -> None:
        self.select_row(0)
        uid = self.window._current.uid_number
        self.window.set_read(True)
        pump(lambda: self.window._store.message("INBOX", uid).is_read)

        index = [f[0] for f in viewer.FILTERS].index("unread")
        self.window._filter.setCurrentIndex(index)
        pump(lambda: self.window._proxy.rowCount() == self.message_count - 1, timeout=3)
        self.assertEqual(self.window._proxy.rowCount(), self.message_count - 1)

    def test_sorting_by_sender_and_direction(self) -> None:
        self.window._sort.setCurrentIndex([f[0] for f in viewer.SORT_FIELDS].index("from"))
        self.window._sort_direction.setChecked(False)     # ascending
        pump(timeout=0.5)
        senders = [m.sender_short for m in self.window._model.emails]
        self.assertEqual(senders, sorted(senders))

        self.window._sort_direction.setChecked(True)      # descending
        pump(timeout=0.5)
        senders = [m.sender_short for m in self.window._model.emails]
        self.assertEqual(senders, sorted(senders, reverse=True))

    def test_sorting_by_subject_keeps_the_selection(self) -> None:
        self.select_row(0)
        uid = self.window._current.uid
        self.window._sort.setCurrentIndex([f[0] for f in viewer.SORT_FIELDS].index("subject"))
        pump(timeout=0.5)
        self.assertEqual(self.window._current.uid, uid)


class ComposeTests(GuiTestCase):
    def test_reply_prefills_the_compose_window(self) -> None:
        self.select_row(0)
        original = self.window._current
        self.window.reply(all_recipients=False)
        pump(lambda: bool(self.window._compose_windows), timeout=3)
        compose = self.window._compose_windows[-1]
        try:
            self.assertIn(original.from_addrs[0].email, compose._to.text())
            self.assertTrue(compose._subject.text().startswith("Re: "))
            self.assertIn("wrote:", compose._editor.toPlainText())
            draft = compose.current_draft()
            self.assertEqual(draft.in_reply_to, original.message_id)
        finally:
            compose.close()
            pump(timeout=0.5)

    def test_forward_carries_the_body(self) -> None:
        self.select_row(0)
        self.window.forward(as_attachment=False)
        pump(lambda: bool(self.window._compose_windows), timeout=3)
        compose = self.window._compose_windows[-1]
        try:
            self.assertTrue(compose._subject.text().startswith("Fwd: "))
            self.assertIn("Forwarded message", compose._editor.toPlainText())
        finally:
            compose.close()
            pump(timeout=0.5)

    def test_sending_stores_a_copy_in_sent(self) -> None:
        from mail_sender import Draft

        self.window._open_compose(Draft(
            to=["someone@example.com"], subject="Integration test",
            body_text="hello from the test", from_address="user@example.com",
        ))
        pump(lambda: bool(self.window._compose_windows), timeout=3)
        compose = self.window._compose_windows[-1]
        with fake_smtp_server() as fake:
            compose.send()
            pump(lambda: bool(fake.instances and fake.instances[0].sent), timeout=8)
            self.assertTrue(fake.instances[0].sent)
        pump(lambda: len(self.server.mailboxes["Sent"]) == 1, timeout=8)
        self.assertEqual(len(self.server.mailboxes["Sent"]), 1)

        from mail_parser import parse_email

        stored = parse_email(self.server.mailboxes["Sent"][0].raw)
        self.assertEqual(stored.subject, "Integration test")

    def test_saving_a_draft_appends_to_drafts(self) -> None:
        from mail_sender import Draft

        self.window._open_compose(Draft(
            to=["draft@example.com"], subject="Unfinished",
            body_text="to be continued", from_address="user@example.com",
        ))
        pump(lambda: bool(self.window._compose_windows), timeout=3)
        compose = self.window._compose_windows[-1]
        compose.save_draft()
        pump(lambda: len(self.server.mailboxes["Drafts"]) == 1, timeout=8)
        self.assertEqual(len(self.server.mailboxes["Drafts"]), 1)
        self.assertIn("\\Draft", self.server.mailboxes["Drafts"][0].flags)
        compose.close()
        pump(timeout=0.5)


class FolderTests(GuiTestCase):
    def test_switching_folders_loads_that_mailbox(self) -> None:
        self.server.mailboxes["Sent"].append(
            FakeMessage(uid=500, raw=sample_message(subject="A sent message"))
        )
        self.window.open_folder("Sent")
        pump(lambda: self.window._model.rowCount() == 1, timeout=8)
        self.assertEqual(self.window._model.rowCount(), 1)
        self.assertEqual(self.window._model.emails[0].subject, "A sent message")

        self.window.open_folder("INBOX")
        pump(lambda: self.window._model.rowCount() == self.message_count, timeout=8)
        self.assertEqual(self.window._model.rowCount(), self.message_count)


class ErrorHandlingTests(unittest.TestCase):
    def test_authentication_failure_is_reported_not_crashed(self) -> None:
        temp = Path(tempfile.mkdtemp(prefix="mailgui-auth-"))
        settings = AppSettings(
            account=AccountSettings(host="imap.example.com", username="user@example.com",
                                    password="wrong-password"),
            path=temp / "config.ini",
        )
        settings.sync.interval_seconds = 0
        settings.sync.cache_dir = str(temp / "cache")
        settings.sync.sync_on_start = True

        with fake_imap_server(mailboxes=mailboxes(1), password="secret"):
            window = viewer.MainWindow(settings)
            window.show()
            pump(lambda: "failed" in window._sync_label.text().lower(), timeout=8)
            self.assertIn("failed", window._sync_label.text().lower())
            self.assertEqual(window._model.rowCount(), 0)
            window.close()
            pump(timeout=1.0)
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
