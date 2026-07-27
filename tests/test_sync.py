"""Synchronisation tests: incremental updates, dedup, caching, failures.

The Qt layer is not involved - :class:`FolderSynchronizer` is plain Python, so
these run headlessly against the in-memory IMAP server.

    python tests/test_sync.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import AccountSettings  # noqa: E402
from fake_imap import FakeMessage, fake_imap_server, sample_message  # noqa: E402
from mail_receiver import ImapClient  # noqa: E402
from mail_storage import MailStore  # noqa: E402
from mail_sync import FolderSynchronizer  # noqa: E402

ACCOUNT = AccountSettings(host="imap.example.com", username="user@example.com",
                          password="secret")


def messages(count: int, start: int = 1) -> list[FakeMessage]:
    return [
        FakeMessage(uid=uid, raw=sample_message(subject=f"Message {uid}", body=f"body {uid}"))
        for uid in range(start, start + count)
    ]


def body_fetches(server) -> int:
    """How many times a message body was actually downloaded."""
    return sum(
        1 for command in server.commands
        if command[0] == "uid" and command[1] == "FETCH" and "BODY.PEEK[]" in str(command[3])
    )


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="mailsync-test-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def store(self, cache: bool = False) -> MailStore:
        return MailStore(cache_dir=self.temp if cache else None,
                         account_key="test", cache_enabled=cache)

    # ------------------------------------------------------------ basic sync
    def test_first_sync_downloads_everything(self) -> None:
        with fake_imap_server(mailboxes={"INBOX": messages(3)}):
            with ImapClient(ACCOUNT) as client:
                store = self.store()
                result = FolderSynchronizer(client, store).sync("INBOX")
        self.assertEqual(len(result.new_messages), 3)
        self.assertEqual(result.total, 3)
        self.assertEqual(result.unread, 3)
        self.assertEqual(len(store.messages("INBOX")), 3)
        self.assertEqual(store.messages("INBOX")[0].subject, "Message 3")

    def test_second_sync_downloads_nothing_new(self) -> None:
        with fake_imap_server(mailboxes={"INBOX": messages(3)}) as server:
            with ImapClient(ACCOUNT) as client:
                store = self.store()
                synchronizer = FolderSynchronizer(client, store)
                synchronizer.sync("INBOX")
                downloads_after_first = body_fetches(server)
                result = synchronizer.sync("INBOX")
        self.assertEqual(len(result.new_messages), 0)
        self.assertFalse(result.changed)
        self.assertEqual(body_fetches(server), downloads_after_first,
                         "a second sync must not re-download known messages")

    def test_new_arrival_is_detected(self) -> None:
        with fake_imap_server(mailboxes={"INBOX": messages(2)}) as server:
            with ImapClient(ACCOUNT) as client:
                store = self.store()
                synchronizer = FolderSynchronizer(client, store)
                synchronizer.sync("INBOX")
                server.mailboxes["INBOX"].append(
                    FakeMessage(uid=99, raw=sample_message(subject="Fresh mail"))
                )
                result = synchronizer.sync("INBOX")
        self.assertEqual([m.subject for m in result.new_messages], ["Fresh mail"])
        self.assertEqual(len(store.messages("INBOX")), 3)

    def test_flag_change_is_detected_without_downloading(self) -> None:
        with fake_imap_server(mailboxes={"INBOX": messages(2)}) as server:
            with ImapClient(ACCOUNT) as client:
                store = self.store()
                synchronizer = FolderSynchronizer(client, store)
                synchronizer.sync("INBOX")
                downloads = body_fetches(server)
                server.touch_flags("INBOX", 1, add={"\\Seen"})
                server.touch_flags("INBOX", 2, add={"\\Flagged"})
                result = synchronizer.sync("INBOX")
        self.assertEqual(len(result.flag_updates), 2)
        self.assertEqual(body_fetches(server), downloads)
        self.assertTrue(store.message("INBOX", 1).is_read)
        self.assertTrue(store.message("INBOX", 2).is_starred)

    def test_second_pass_is_incremental_and_asks_only_for_new_uids(self) -> None:
        """The point of the local index: the server is not asked about mail we
        already have."""
        with fake_imap_server(mailboxes={"INBOX": messages(5)}) as server:
            with ImapClient(ACCOUNT) as client:
                store = self.store()
                synchronizer = FolderSynchronizer(client, store)
                synchronizer.sync("INBOX")

                before = len(server.commands)
                server.add_message("INBOX", FakeMessage(
                    uid=42, raw=sample_message(subject="Brand new")))
                result = synchronizer.sync("INBOX")

        self.assertTrue(result.incremental)
        self.assertEqual([m.subject for m in result.new_messages], ["Brand new"])

        # (message set, spec) of every metadata FETCH of the second pass.
        fetches = [(str(command[2]), str(command[3])) for command in server.commands[before:]
                   if command[0] == "uid" and command[1] == "FETCH"
                   and "BODY" not in str(command[3])]
        self.assertTrue(fetches, "no status fetch was issued")
        self.assertTrue(any(message_set.startswith("6:") for message_set, _ in fetches),
                        f"expected a 6:* style range, got {fetches}")
        # A whole-mailbox range is only acceptable with CHANGEDSINCE, which
        # makes the server answer with just the messages that changed.
        unqualified = [message_set for message_set, spec in fetches
                       if message_set == "1:*" and "CHANGEDSINCE" not in spec.upper()]
        self.assertEqual(unqualified, [],
                         "the mailbox must not be scanned in full any more")

    def test_full_scan_when_the_server_has_no_condstore(self) -> None:
        with fake_imap_server(mailboxes={"INBOX": messages(3)},
                              capabilities=("IMAP4REV1", "MOVE")) as server:
            with ImapClient(ACCOUNT) as client:
                store = self.store()
                synchronizer = FolderSynchronizer(client, store)
                synchronizer.sync("INBOX")
                server.touch_flags("INBOX", 1, add={"\\Seen"})
                result = synchronizer.sync("INBOX")
        self.assertFalse(result.incremental)
        self.assertEqual(len(result.flag_updates), 1)

    def test_removed_message_is_dropped(self) -> None:
        with fake_imap_server(mailboxes={"INBOX": messages(3)}) as server:
            with ImapClient(ACCOUNT) as client:
                store = self.store()
                synchronizer = FolderSynchronizer(client, store)
                synchronizer.sync("INBOX")
                server.mailboxes["INBOX"] = [
                    m for m in server.mailboxes["INBOX"] if m.uid != 2
                ]
                result = synchronizer.sync("INBOX")
        self.assertEqual(result.removed_uids, [2])
        self.assertIsNone(store.message("INBOX", 2))
        self.assertEqual(len(store.messages("INBOX")), 2)

    def test_filtered_sync_never_reports_removals(self) -> None:
        """`unread` shows a subset; read messages must not look deleted."""
        with fake_imap_server(mailboxes={"INBOX": messages(3)}) as server:
            with ImapClient(ACCOUNT) as client:
                store = self.store()
                synchronizer = FolderSynchronizer(client, store)
                synchronizer.sync("INBOX")
                server.mailboxes["INBOX"][0].flags.add("\\Seen")
                result = synchronizer.sync("INBOX", criteria="unread")
        self.assertEqual(result.removed_uids, [])
        self.assertEqual(len(store.messages("INBOX")), 3)

    def test_max_messages_caps_the_first_download(self) -> None:
        with fake_imap_server(mailboxes={"INBOX": messages(10)}):
            with ImapClient(ACCOUNT) as client:
                store = self.store()
                result = FolderSynchronizer(client, store, max_messages=4).sync("INBOX")
        self.assertEqual(len(result.new_messages), 4)
        # Newest first: UIDs 10..7
        self.assertEqual(sorted(m.uid_number for m in result.new_messages), [7, 8, 9, 10])

    def test_cancellation_stops_the_download(self) -> None:
        with fake_imap_server(mailboxes={"INBOX": messages(5)}):
            with ImapClient(ACCOUNT) as client:
                store = self.store()
                state = {"count": 0}

                def stop() -> bool:
                    return state["count"] >= 2

                def on_message(_mail) -> None:
                    state["count"] += 1

                result = FolderSynchronizer(client, store).sync(
                    "INBOX", should_stop=stop, on_message=on_message
                )
        self.assertTrue(result.cancelled)
        self.assertEqual(len(result.new_messages), 2)

    # ---------------------------------------------------------------- caching
    def test_restart_downloads_nothing_and_still_shows_everything(self) -> None:
        """After a restart the index already knows the mailbox.

        Before the index existed this re-parsed every cached ``.eml``; now the
        second run recognises the UIDs and asks the server for nothing at all.
        """
        with fake_imap_server(mailboxes={"INBOX": messages(3)}) as server:
            with ImapClient(ACCOUNT) as client:
                first_store = self.store(cache=True)
                FolderSynchronizer(client, first_store).sync("INBOX")
                first_store.close()
                downloads = body_fetches(server)

                # "Restart": a brand new store over the same cache directory.
                second_store = self.store(cache=True)
                result = FolderSynchronizer(client, second_store).sync("INBOX")

        self.assertEqual(len(result.new_messages), 0, "nothing is new after a restart")
        self.assertEqual(len(second_store.messages("INBOX")), 3,
                         "the list is restored from the index")
        self.assertEqual(body_fetches(server), downloads,
                         "known messages must not be downloaded again")

    def test_a_body_is_parsed_only_when_the_message_is_opened(self) -> None:
        with fake_imap_server(mailboxes={"INBOX": messages(2)}):
            with ImapClient(ACCOUNT) as client:
                store = self.store(cache=True)
                FolderSynchronizer(client, store).sync("INBOX")
                store.close()

        restored = self.store(cache=True)
        summary = restored.messages("INBOX")[0]
        self.assertFalse(summary.loaded)
        self.assertTrue(summary.subject)          # headers come from the index
        self.assertTrue(summary.preview())        # so does the snippet
        self.assertEqual(summary.text_body, "")   # but no MIME was parsed

        full = restored.ensure_loaded(summary)
        self.assertTrue(full.loaded)
        self.assertIn("body", full.text_body)

    def test_load_from_cache_without_network(self) -> None:
        with fake_imap_server(mailboxes={"INBOX": messages(2)}):
            with ImapClient(ACCOUNT) as client:
                store = self.store(cache=True)
                FolderSynchronizer(client, store).sync("INBOX")

        offline_store = self.store(cache=True)
        # No server at all now: the client would fail if it were used.
        synchronizer = FolderSynchronizer(ImapClient(ACCOUNT), offline_store)
        loaded = synchronizer.load_from_cache("INBOX")
        self.assertEqual(len(loaded), 2)
        self.assertEqual(len(offline_store.messages("INBOX")), 2)

    def test_uidvalidity_change_clears_the_folder(self) -> None:
        with fake_imap_server(mailboxes={"INBOX": messages(2)}) as server:
            with ImapClient(ACCOUNT) as client:
                store = self.store(cache=True)
                synchronizer = FolderSynchronizer(client, store)
                synchronizer.sync("INBOX")
                self.assertEqual(len(store.messages("INBOX")), 2)

                server.uid_validity = 4242          # mailbox was recreated
                server.mailboxes["INBOX"] = messages(1, start=1)
                result = synchronizer.sync("INBOX")

        self.assertEqual(len(result.new_messages), 1)
        self.assertEqual(len(store.messages("INBOX")), 1)

    def test_cache_prune_keeps_the_newest(self) -> None:
        with fake_imap_server(mailboxes={"INBOX": messages(6)}):
            with ImapClient(ACCOUNT) as client:
                store = MailStore(cache_dir=self.temp, account_key="test",
                                  cache_enabled=True, max_messages_per_folder=10)
                FolderSynchronizer(client, store).sync("INBOX")
                store._max_per_folder = 3
                removed = store.prune_cache("INBOX")
        self.assertEqual(removed, 3)
        self.assertEqual(store.cached_uids("INBOX"), [6, 5, 4])

    # ----------------------------------------------------------------- errors
    def test_authentication_failure_is_returned_not_raised(self) -> None:
        with fake_imap_server(mailboxes={"INBOX": []}, password="different"):
            store = self.store()
            result = FolderSynchronizer(ImapClient(ACCOUNT), store).sync("INBOX")
        self.assertTrue(result.error)
        self.assertIn("Login failed", result.error)
        self.assertEqual(result.new_messages, [])

    def test_unreadable_message_does_not_abort_the_sync(self) -> None:
        broken = FakeMessage(uid=2, raw=b"")     # server returns an empty body
        with fake_imap_server(mailboxes={"INBOX": [messages(1)[0], broken, messages(1, 3)[0]]}):
            with ImapClient(ACCOUNT) as client:
                store = self.store()
                result = FolderSynchronizer(client, store).sync("INBOX")
        self.assertFalse(result.error)
        self.assertGreaterEqual(len(result.new_messages), 2)

    def test_folder_counts_are_recorded(self) -> None:
        inbox = messages(4)
        inbox[0].flags.add("\\Seen")
        with fake_imap_server(mailboxes={"INBOX": inbox}):
            with ImapClient(ACCOUNT) as client:
                store = self.store()
                result = FolderSynchronizer(client, store).sync("INBOX")
        self.assertEqual(result.total, 4)
        self.assertEqual(result.unread, 3)
        self.assertEqual(store.folder("INBOX").unread, 3)


class StoreTests(unittest.TestCase):
    def test_add_reports_new_versus_known(self) -> None:
        from mail_parser import parse_email

        store = MailStore(cache_enabled=False)
        mail = parse_email(sample_message(subject="One"), uid="1", folder="INBOX")
        self.assertTrue(store.add(mail))
        self.assertFalse(store.add(mail))

    def test_messages_are_sorted_newest_first(self) -> None:
        from mail_parser import parse_email

        store = MailStore(cache_enabled=False)
        for uid, date in ((1, "Mon, 20 Jul 2026 10:00:00 +0000"),
                          (2, "Wed, 22 Jul 2026 10:00:00 +0000")):
            raw = (f"From: a@b.c\r\nSubject: M{uid}\r\nDate: {date}\r\n\r\nbody\r\n").encode()
            store.add(parse_email(raw, uid=str(uid), folder="INBOX"))
        self.assertEqual([m.subject for m in store.messages("INBOX")], ["M2", "M1"])

    def test_move_message_between_folders(self) -> None:
        from mail_parser import parse_email

        store = MailStore(cache_enabled=False)
        store.add(parse_email(sample_message(), uid="7", folder="INBOX"))
        store.move_message("INBOX", 7, "Trash")
        self.assertIsNone(store.message("INBOX", 7))
        self.assertIsNotNone(store.message("Trash", 7))


if __name__ == "__main__":
    unittest.main(verbosity=2)
