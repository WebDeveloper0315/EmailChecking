"""Outbox queue and multi-account configuration.

    python tests/test_outbox_config.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import AccountProfile, AccountSettings, AppSettings, SmtpSettings  # noqa: E402
from outbox import MAX_ATTEMPTS, Outbox  # noqa: E402


class OutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="outbox-test-"))
        self.outbox = Outbox(self.temp)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def add(self, subject: str = "Hello", account: str = "Personal", error: str = ""):
        return self.outbox.add(b"From: me@x.com\r\n\r\nbody", "me@x.com",
                               ["you@x.com"], account, subject=subject, error=error)

    def test_queued_message_survives_a_restart(self) -> None:
        item = self.add("Persisted")
        reopened = Outbox(self.temp)          # a new process would do this
        items = reopened.all()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].identifier, item.identifier)
        self.assertEqual(items[0].subject, "Persisted")
        self.assertEqual(items[0].recipients, ["you@x.com"])
        self.assertEqual(items[0].raw, b"From: me@x.com\r\n\r\nbody")

    def test_a_fresh_entry_is_due_immediately(self) -> None:
        self.add()
        self.assertEqual(len(self.outbox.due()), 1)

    def test_failure_pushes_the_next_attempt_into_the_future(self) -> None:
        item = self.add()
        self.outbox.record_failure(item, "server unreachable")
        self.assertEqual(item.attempts, 1)
        self.assertFalse(item.due)
        self.assertEqual(self.outbox.due(), [])
        self.assertEqual(self.outbox.all()[0].last_error, "server unreachable")

    def test_backoff_grows_but_is_capped(self) -> None:
        item = self.add()
        delays: list[float] = []
        for _ in range(8):
            before = datetime.now(timezone.utc)
            self.outbox.record_failure(item, "nope")
            after = datetime.fromisoformat(item.next_attempt)
            delays.append((after - before).total_seconds())
        self.assertLess(delays[0], delays[3])
        self.assertLessEqual(max(delays), timedelta(minutes=30).total_seconds() + 5)

    def test_due_filters_by_account(self) -> None:
        self.add(account="Personal")
        self.add(account="Work")
        self.assertEqual(len(self.outbox.due("Personal")), 1)
        self.assertEqual(len(self.outbox.due("Work")), 1)
        self.assertEqual(len(self.outbox.due()), 2)

    def test_exhausted_entries_stop_being_retried(self) -> None:
        import json

        item = self.add()
        # Persist an entry that has failed too often but is otherwise due.
        item.attempts = MAX_ATTEMPTS
        item.next_attempt = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        (self.temp / f"{item.identifier}.json").write_text(
            json.dumps(item.metadata()), encoding="utf-8")

        stored = self.outbox.all()[0]
        self.assertTrue(stored.exhausted)
        self.assertTrue(stored.due)
        self.assertEqual(self.outbox.due(), [],
                         "a message that failed 20 times must stop being retried")

    def test_remove_and_clear(self) -> None:
        first = self.add("one")
        self.add("two")
        self.assertEqual(self.outbox.count(), 2)
        self.outbox.remove(first)
        self.assertEqual(self.outbox.count(), 1)
        self.assertEqual(self.outbox.clear(), 1)
        self.assertEqual(self.outbox.count(), 0)

    def test_a_body_without_metadata_is_ignored(self) -> None:
        (self.temp / "orphan.eml").write_bytes(b"stray")
        self.assertEqual(self.outbox.all(), [])

    def test_metadata_never_contains_the_raw_bytes(self) -> None:
        item = self.add()
        self.assertNotIn("raw", item.metadata())


class MultiAccountConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="config-test-"))
        self.path = self.temp / "config.ini"

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)
        for name in ("MAIL_USERNAME", "MAIL_PASSWORD", "IMAP_SERVER"):
            os.environ.pop(name, None)

    def build(self) -> AppSettings:
        settings = AppSettings(profiles=[], path=self.path)
        settings.add_profile(AccountProfile(
            name="Personal",
            account=AccountSettings(host="imap.gmail.com", username="me@gmail.com",
                                    password="secret1"),
            smtp=SmtpSettings(from_name="Me"),
        ))
        settings.add_profile(AccountProfile(
            name="Work",
            account=AccountSettings(host="outlook.office365.com", username="me@work.com",
                                    password="secret2"),
        ))
        settings.active_profile = "Work"
        settings.remember_password = True
        return settings

    def test_round_trip_keeps_every_account(self) -> None:
        self.build().save()
        loaded = AppSettings.load(self.path)
        self.assertEqual([p.name for p in loaded.profiles], ["Personal", "Work"])
        self.assertEqual(loaded.active_profile, "Work")
        self.assertEqual(loaded.profile.account.username, "me@work.com")
        self.assertEqual(loaded.find_profile("Personal").account.password, "secret1")
        self.assertEqual(loaded.find_profile("Personal").smtp.from_name, "Me")

    def test_passwords_are_not_written_unless_asked(self) -> None:
        settings = self.build()
        settings.remember_password = False
        settings.save()
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("secret1", text)
        self.assertNotIn("secret2", text)

    def test_old_single_account_file_is_migrated(self) -> None:
        self.path.write_text(
            "[account]\n"
            "host = imap.gmail.com\n"
            "port = 993\n"
            "username = legacy@gmail.com\n"
            "password = legacypass\n"
            "\n[smtp]\n"
            "host = smtp.gmail.com\n"
            "from_name = Legacy\n"
            "\n[viewer]\n"
            "mark_seen = True\n",
            encoding="utf-8",
        )
        loaded = AppSettings.load(self.path)
        self.assertEqual(len(loaded.profiles), 1)
        profile = loaded.profiles[0]
        self.assertEqual(profile.name, "legacy@gmail.com")
        self.assertEqual(profile.account.username, "legacy@gmail.com")
        self.assertEqual(profile.account.password, "legacypass")
        self.assertEqual(profile.smtp.host, "smtp.gmail.com")
        self.assertEqual(profile.smtp.from_name, "Legacy")
        self.assertTrue(loaded.mark_seen)
        # The compatibility properties still work.
        self.assertEqual(loaded.account.username, "legacy@gmail.com")

    def test_profiles_have_distinct_cache_keys(self) -> None:
        settings = self.build()
        keys = {p.key for p in settings.profiles}
        self.assertEqual(len(keys), 2)

    def test_profile_lookup_by_address(self) -> None:
        settings = self.build()
        self.assertEqual(settings.profile_for_address("me@work.com").name, "Work")
        self.assertEqual(settings.profile_for_address("ME@GMAIL.COM").name, "Personal")
        self.assertIsNone(settings.profile_for_address("nobody@example.com"))

    def test_adding_a_duplicate_name_is_made_unique(self) -> None:
        settings = self.build()
        added = settings.add_profile(AccountProfile(name="Work"))
        self.assertNotEqual(added.name, "Work")
        self.assertEqual(len({p.name for p in settings.profiles}), 3)

    def test_removing_the_active_profile_switches_over(self) -> None:
        settings = self.build()
        self.assertTrue(settings.remove_profile("Work"))
        self.assertEqual(settings.active_profile, "Personal")
        self.assertFalse(settings.remove_profile("Personal"), "the last account stays")

    def test_environment_overrides_only_the_active_profile(self) -> None:
        self.build().save()
        os.environ["MAIL_USERNAME"] = "env@example.com"
        os.environ["MAIL_PASSWORD"] = "envpass"
        loaded = AppSettings.load(self.path)
        self.assertEqual(loaded.profile.account.username, "env@example.com")
        self.assertEqual(loaded.find_profile("Personal").account.username, "me@gmail.com")

    def test_enabled_profiles_skips_incomplete_accounts(self) -> None:
        settings = self.build()
        settings.add_profile(AccountProfile(name="Empty"))
        self.assertEqual({p.name for p in settings.enabled_profiles()}, {"Personal", "Work"})

    def test_smtp_falls_back_to_the_imap_credentials_per_profile(self) -> None:
        settings = self.build()
        work = settings.smtp_settings(settings.find_profile("Work"))
        self.assertEqual(work.host, "smtp.office365.com")
        self.assertEqual(work.username, "me@work.com")
        self.assertEqual(work.password, "secret2")

    def test_notification_settings_round_trip(self) -> None:
        settings = self.build()
        settings.notifications.enabled = False
        settings.notifications.play_sound = True
        settings.notifications.minimize_to_tray = False
        settings.sync.outbox_retry_seconds = 240
        settings.save()
        loaded = AppSettings.load(self.path)
        self.assertFalse(loaded.notifications.enabled)
        self.assertTrue(loaded.notifications.play_sound)
        self.assertFalse(loaded.notifications.minimize_to_tray)
        self.assertEqual(loaded.sync.outbox_retry_seconds, 240)


if __name__ == "__main__":
    unittest.main(verbosity=2)
