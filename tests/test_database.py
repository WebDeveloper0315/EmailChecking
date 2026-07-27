"""The SQLite index and the store built on it.

    python tests/test_database.py
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

from fake_imap import sample_message  # noqa: E402
from mail_database import MailDatabase, MessageRecord  # noqa: E402
from mail_parser import parse_email  # noqa: E402
from mail_storage import MailStore  # noqa: E402


def record(uid: int, subject: str = "Subject", date: str = "2026-07-24T09:15:00+00:00",
           flags: frozenset[str] = frozenset()) -> MessageRecord:
    return MessageRecord(account="acc", folder="INBOX", uid=uid, subject=subject,
                         sender_name="Sender", sender_email="s@example.com",
                         date_iso=date, size=1234, flags=flags, preview="a snippet",
                         attachments=1, search_blob=f"{subject} s@example.com")


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="maildb-test-"))
        self.db = MailDatabase(self.temp / "index.sqlite3")

    def tearDown(self) -> None:
        self.db.close()
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_upsert_reports_new_versus_known(self) -> None:
        self.assertTrue(self.db.upsert(record(1)))
        self.assertFalse(self.db.upsert(record(1, subject="Edited")))
        stored = self.db.record("acc", "INBOX", 1)
        self.assertEqual(stored.subject, "Edited")

    def test_rows_come_back_newest_first(self) -> None:
        self.db.upsert(record(1, "old", "2026-07-01T10:00:00+00:00"))
        self.db.upsert(record(2, "new", "2026-07-20T10:00:00+00:00"))
        self.assertEqual([r.subject for r in self.db.records("acc", "INBOX")],
                         ["new", "old"])

    def test_highest_uid_tracks_the_index(self) -> None:
        self.db.upsert(record(5))
        self.db.upsert(record(3))
        self.assertEqual(self.db.folder_state("acc", "INBOX").highest_uid, 5)

    def test_sync_bookmarks_survive_reopening(self) -> None:
        from mail_database import FolderState

        self.db.save_folder_state(FolderState(account="acc", folder="INBOX",
                                              uid_validity=99, highest_uid=120,
                                              mod_sequence=4242, total=7, unread=3))
        self.db.close()

        reopened = MailDatabase(self.temp / "index.sqlite3")
        state = reopened.folder_state("acc", "INBOX")
        self.assertEqual(state.uid_validity, 99)
        self.assertEqual(state.highest_uid, 120)
        self.assertEqual(state.mod_sequence, 4242)
        reopened.close()

    def test_flags_round_trip_and_report_changes(self) -> None:
        self.db.upsert(record(1))
        self.assertTrue(self.db.set_flags("acc", "INBOX", 1, {"\\Seen"}))
        self.assertFalse(self.db.set_flags("acc", "INBOX", 1, {"\\Seen"}))
        self.assertEqual(self.db.record("acc", "INBOX", 1).flags, frozenset({"\\Seen"}))

    def test_counts_and_unread(self) -> None:
        self.db.upsert(record(1, flags=frozenset({"\\Seen"})))
        self.db.upsert(record(2))
        self.assertEqual(self.db.counts("acc", "INBOX"), (2, 1))

    def test_delete_and_reset(self) -> None:
        self.db.upsert(record(1))
        self.db.upsert(record(2))
        self.assertEqual(self.db.delete("acc", "INBOX", [1]), [1])
        self.assertEqual(self.db.uids("acc", "INBOX"), {2})
        self.db.reset_folder("acc", "INBOX", 100)
        self.assertEqual(self.db.uids("acc", "INBOX"), set())
        self.assertEqual(self.db.folder_state("acc", "INBOX").highest_uid, 0)

    def test_move_between_folders(self) -> None:
        self.db.upsert(record(1))
        self.db.move("acc", "INBOX", 1, "Trash")
        self.assertEqual(self.db.uids("acc", "INBOX"), set())
        self.assertEqual(self.db.uids("acc", "Trash"), {1})

    def test_prune_keeps_the_newest(self) -> None:
        for uid in range(1, 6):
            self.db.upsert(record(uid, f"m{uid}", f"2026-07-0{uid}T10:00:00+00:00"))
        stale = self.db.prune("acc", "INBOX", 2)
        self.assertEqual(sorted(stale), [1, 2, 3])
        self.assertEqual(self.db.uids("acc", "INBOX"), {4, 5})

    def test_accounts_do_not_see_each_other(self) -> None:
        self.db.upsert(record(1))
        other = MessageRecord(account="other", folder="INBOX", uid=1, subject="theirs")
        self.db.upsert(other)
        self.assertEqual(len(self.db.records("acc", "INBOX")), 1)
        self.assertEqual(self.db.records("other", "INBOX")[0].subject, "theirs")
        self.db.clear_account("other")
        self.assertEqual(self.db.records("other", "INBOX"), [])
        self.assertEqual(len(self.db.records("acc", "INBOX")), 1)

    def test_a_closed_index_answers_instead_of_raising(self) -> None:
        """Qt can deliver a queued signal after the window closed the store."""
        self.db.upsert(record(1))
        self.db.close()
        self.assertEqual(self.db.records("acc", "INBOX"), [])
        self.assertEqual(self.db.counts("acc", "INBOX"), (0, 0))
        self.assertEqual(self.db.uids("acc", "INBOX"), set())
        self.assertIsNone(self.db.record("acc", "INBOX", 1))
        self.assertFalse(self.db.upsert(record(2)))
        self.assertEqual(self.db.delete("acc", "INBOX", [1]), [])


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="mailstore-test-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def store(self) -> MailStore:
        return MailStore(cache_dir=self.temp, account_key="acc", cache_enabled=True)

    def message(self, uid: int, subject: str = "Hello"):
        raw = sample_message(subject=subject, body=f"body of {uid}")
        mail = parse_email(raw, uid=str(uid), folder="INBOX")
        return raw, mail

    def test_add_indexes_and_caches(self) -> None:
        store = self.store()
        raw, mail = self.message(1)
        store.store_raw("INBOX", 1, raw)
        self.assertTrue(store.add(mail))
        self.assertFalse(store.add(mail))
        self.assertEqual(store.cached_raw("INBOX", 1), raw)
        self.assertEqual(len(store.messages("INBOX")), 1)
        store.close()

    def test_summaries_survive_a_restart_without_parsing(self) -> None:
        store = self.store()
        raw, mail = self.message(7, "Persisted subject")
        store.store_raw("INBOX", 7, raw)
        store.add(mail)
        store.close()

        reopened = self.store()
        summary = reopened.messages("INBOX")[0]
        self.assertEqual(summary.subject, "Persisted subject")
        self.assertEqual(summary.sender_short, "sender@example.com")
        self.assertFalse(summary.loaded)
        self.assertEqual(summary.text_body, "")
        self.assertIn("body of 7", summary.preview())

        full = reopened.ensure_loaded(summary)
        self.assertTrue(full.loaded)
        self.assertIn("body of 7", full.text_body)
        reopened.close()

    def test_known_uids_and_highest_uid_come_from_the_index(self) -> None:
        store = self.store()
        for uid in (3, 9, 4):
            raw, mail = self.message(uid)
            store.store_raw("INBOX", uid, raw)
            store.add(mail)
        self.assertEqual(store.known_uids("INBOX"), {3, 4, 9})
        self.assertEqual(store.highest_uid("INBOX"), 9)
        store.close()

    def test_flags_update_both_index_and_loaded_copy(self) -> None:
        store = self.store()
        raw, mail = self.message(1)
        store.store_raw("INBOX", 1, raw)
        store.add(mail)
        self.assertTrue(store.update_flags("INBOX", 1, {"\\Seen"}))
        self.assertTrue(store.message("INBOX", 1).is_read)
        self.assertFalse(store.update_flags("INBOX", 1, {"\\Seen"}))
        store.close()

    def test_remove_drops_index_row_and_cached_body(self) -> None:
        store = self.store()
        raw, mail = self.message(1)
        store.store_raw("INBOX", 1, raw)
        store.add(mail)
        self.assertEqual(store.remove("INBOX", [1]), [1])
        self.assertIsNone(store.cached_raw("INBOX", 1))
        self.assertEqual(store.messages("INBOX"), [])
        store.close()

    def test_uid_validity_change_wipes_the_folder(self) -> None:
        store = self.store()
        raw, mail = self.message(1)
        store.store_raw("INBOX", 1, raw)
        store.add(mail)
        store.set_sync_state("INBOX", uid_validity=10)
        self.assertFalse(store.set_validity("INBOX", 10))
        self.assertTrue(store.set_validity("INBOX", 11))
        self.assertEqual(store.messages("INBOX"), [])
        self.assertIsNone(store.cached_raw("INBOX", 1))
        store.close()

    def test_prune_removes_the_oldest_from_index_and_disk(self) -> None:
        # 10 is the floor the store enforces on the per-folder cap.
        store = MailStore(cache_dir=self.temp, account_key="acc", cache_enabled=True,
                          max_messages_per_folder=10)
        for uid in range(1, 14):
            raw = sample_message(subject=f"m{uid}")
            mail = parse_email(raw, uid=str(uid), folder="INBOX")
            mail.date = None                    # equal dates -> ordered by uid
            store.store_raw("INBOX", uid, raw)
            store.add(mail)
        store.prune_cache("INBOX")
        self.assertEqual(store.known_uids("INBOX"), set(range(4, 14)))
        self.assertEqual(sorted(store.cached_uids("INBOX")), list(range(4, 14)))
        store.close()

    def test_a_cache_from_an_older_version_is_adopted(self) -> None:
        """Renaming an account (or upgrading) must not re-download everything."""
        legacy = self.temp / "Personal-me@example.com"
        (legacy / "INBOX").mkdir(parents=True)
        (legacy / "INBOX" / "1.eml").write_bytes(sample_message(subject="Old cache"))

        store = MailStore(cache_dir=self.temp, account_key="me@example.com@imap.example.com",
                          cache_enabled=True, legacy_keys=("Personal-me@example.com",))
        self.assertFalse(legacy.exists(), "the old directory was left behind")
        self.assertIsNotNone(store.cached_raw("INBOX", 1))
        store.close()

    def test_an_existing_cache_is_never_overwritten_by_a_legacy_one(self) -> None:
        current = self.temp / "me@example.com@imap.example.com"
        (current / "INBOX").mkdir(parents=True)
        legacy = self.temp / "Personal-me@example.com"
        legacy.mkdir()

        store = MailStore(cache_dir=self.temp, account_key="me@example.com@imap.example.com",
                          cache_enabled=True, legacy_keys=("Personal-me@example.com",))
        self.assertTrue(legacy.exists())
        self.assertTrue(current.exists())
        store.close()

    def test_the_account_key_ignores_the_profile_name(self) -> None:
        from config import AccountProfile, AccountSettings

        profile = AccountProfile(name="Personal",
                                 account=AccountSettings(host="imap.example.com",
                                                         username="me@example.com"))
        before = profile.key
        profile.name = "Renamed"
        self.assertEqual(profile.key, before)
        self.assertIn("Renamed-me@example.com", profile.legacy_keys)

    def test_store_works_without_a_cache_directory(self) -> None:
        store = MailStore(cache_enabled=False)
        raw, mail = self.message(1)
        self.assertTrue(store.add(mail))
        self.assertEqual(len(store.messages("INBOX")), 1)
        self.assertIsNone(store.cached_raw("INBOX", 1))
        store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
