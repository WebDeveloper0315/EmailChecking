"""Logging tests: structured fields survive, secrets never do.

    python tests/test_logging.py
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging_setup  # noqa: E402


class LoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="maillog-test-"))
        self.log_path = logging_setup.configure("DEBUG", self.temp, console=False)
        self.assertIsNotNone(self.log_path)

    def tearDown(self) -> None:
        for handler in list(logging.getLogger().handlers):
            handler.close()
            logging.getLogger().removeHandler(handler)
        logging_setup.SecretRedactingFilter._secrets.clear()
        shutil.rmtree(self.temp, ignore_errors=True)

    def records(self) -> list[dict]:
        for handler in logging.getLogger().handlers:
            handler.flush()
        return [json.loads(line) for line in
                Path(self.log_path).read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_structured_fields_are_kept(self) -> None:
        """Regression: LoggerAdapter used to drop the caller's extra fields."""
        logger = logging_setup.get_logger("imap")
        logger.info("Synced %s", "INBOX",
                    extra={"event": "sync_done", "folder": "INBOX", "new": 3})
        record = self.records()[-1]
        self.assertEqual(record["component"], "imap")
        self.assertEqual(record["event"], "sync_done")
        self.assertEqual(record["folder"], "INBOX")
        self.assertEqual(record["new"], 3)
        self.assertEqual(record["message"], "Synced INBOX")

    def test_registered_secret_is_masked_everywhere(self) -> None:
        logging_setup.register_secret("hunter2-app-password")
        logger = logging_setup.get_logger("smtp")
        logger.info("Login with hunter2-app-password for %s", "hunter2-app-password",
                    extra={"detail": "token hunter2-app-password"})
        blob = json.dumps(self.records()[-1])
        self.assertNotIn("hunter2-app-password", blob)
        self.assertIn(logging_setup.REDACTED, blob)

    def test_password_shaped_text_is_masked_even_if_unregistered(self) -> None:
        logger = logging_setup.get_logger("config")
        logger.warning("connecting with password=SuperSecret123 and pwd: other456")
        message = self.records()[-1]["message"]
        self.assertNotIn("SuperSecret123", message)
        self.assertNotIn("other456", message)

    def test_imap_login_command_is_masked(self) -> None:
        logger = logging_setup.get_logger("imap")
        logger.debug("> LOGIN user@example.com TopSecretValue")
        self.assertNotIn("TopSecretValue", self.records()[-1]["message"])

    def test_secret_named_extra_keys_are_masked(self) -> None:
        logger = logging_setup.get_logger("smtp")
        logger.info("settings", extra={"password": "abc12345", "host": "smtp.example.com"})
        record = self.records()[-1]
        self.assertEqual(record["password"], logging_setup.REDACTED)
        self.assertEqual(record["host"], "smtp.example.com")

    def test_account_redaction_helper_omits_the_password(self) -> None:
        from config import AccountSettings, SmtpSettings

        account = AccountSettings(host="imap.example.com", username="u@example.com",
                                  password="secret-value")
        self.assertNotIn("secret-value", json.dumps(account.redacted()))
        smtp = SmtpSettings(host="smtp.example.com", username="u@example.com",
                            password="secret-value")
        self.assertNotIn("secret-value", json.dumps(smtp.redacted()))

    def test_exceptions_are_recorded(self) -> None:
        logger = logging_setup.get_logger("sync")
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("failed", extra={"event": "crash"})
        record = self.records()[-1]
        self.assertIn("ValueError: boom", record["exception"])
        self.assertEqual(record["event"], "crash")


if __name__ == "__main__":
    unittest.main(verbosity=2)
