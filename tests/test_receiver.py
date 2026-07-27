"""IMAP layer tests, run against the in-memory server in ``fake_imap.py``.

These cover the bug that made fetching fail (imbox API mismatch) plus every
IMAP operation the client performs, so the retrieval path is verified without
credentials or network access.

    python tests/test_receiver.py
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import AccountSettings  # noqa: E402
from fake_imap import FakeMessage, fake_imap_server, sample_message  # noqa: E402
from mail_parser import parse_email  # noqa: E402
from mail_receiver import (  # noqa: E402
    AuthenticationError,
    ImapClient,
    ReceiveError,
    imap_utf7_decode,
    imap_utf7_encode,
    quote_mailbox,
)

ACCOUNT = AccountSettings(host="imap.example.com", port=993,
                          username="user@example.com", password="secret")


def inbox(count: int = 3) -> list[FakeMessage]:
    return [
        FakeMessage(uid=index,
                    raw=sample_message(subject=f"Message {index}", body=f"body {index}"),
                    flags={"\\Seen"} if index % 2 == 0 else set())
        for index in range(1, count + 1)
    ]


class ConnectionTests(unittest.TestCase):
    def test_connect_with_installed_imbox(self) -> None:
        """Regression test for the reported failure.

        Before the fix this raised
        ``TypeError: Imbox.__init__() got an unexpected keyword argument
        'username'`` because imbox 0.10 replaced the keyword API with a Config
        object.
        """
        with fake_imap_server(mailboxes={"INBOX": inbox()}) as server:
            client = ImapClient(ACCOUNT)
            client.connect()
            self.assertTrue(client.is_connected)
            self.assertTrue(server.logged_in)
            client.logout()
            self.assertTrue(server.logged_out)

    def test_legacy_imbox_signature_is_still_supported(self) -> None:
        """imbox 0.9.x (what the notebook used) must keep working."""
        import mail_receiver

        captured: dict[str, object] = {}

        class LegacyImbox:  # the 0.9.x signature
            def __init__(self, hostname, username=None, password=None, ssl=True,
                         ssl_context=None, starttls=False):
                captured.update(hostname=hostname, username=username,
                                password=password, ssl=ssl, starttls=starttls)
                self.connection = None

        import imbox

        original = imbox.Imbox
        imbox.Imbox = LegacyImbox
        try:
            result = mail_receiver._open_imbox(ACCOUNT)
        finally:
            imbox.Imbox = original

        self.assertIsInstance(result, LegacyImbox)
        self.assertEqual(captured["hostname"], "imap.example.com")
        self.assertEqual(captured["username"], "user@example.com")

    def test_socket_timeout_is_applied(self) -> None:
        with fake_imap_server(mailboxes={"INBOX": inbox()}) as server:
            client = ImapClient(AccountSettings(**{**ACCOUNT.__dict__, "timeout": 17}))
            client.connect()
            self.assertEqual(server.socket().timeout, 17)
            client.logout()

    def test_wrong_password_raises_authentication_error(self) -> None:
        with fake_imap_server(mailboxes={"INBOX": []}, password="other"):
            client = ImapClient(ACCOUNT)
            with self.assertRaises(AuthenticationError) as caught:
                client.connect()
            self.assertIn("Login failed", str(caught.exception))

    def test_missing_credentials_are_reported_before_connecting(self) -> None:
        client = ImapClient(AccountSettings(host="imap.example.com"))
        with self.assertRaises(AuthenticationError):
            client.connect()

    def test_unknown_host_is_translated(self) -> None:
        import socket

        import mail_receiver

        error = mail_receiver._translate(socket.gaierror("getaddrinfo failed"), ACCOUNT)
        self.assertIsInstance(error, ReceiveError)
        self.assertIn("Cannot find the server", str(error))


class SearchAndFetchTests(unittest.TestCase):
    def test_search_all_uses_imbox_and_returns_uids(self) -> None:
        with fake_imap_server(mailboxes={"INBOX": inbox(4)}) as server:
            with ImapClient(ACCOUNT) as client:
                self.assertEqual(client.search_uids("INBOX", "all"), [1, 2, 3, 4])
            searches = [c for c in server.commands if c[0] == "uid" and c[1] == "SEARCH"]
            self.assertTrue(searches, "the search must go through imbox")

    def test_search_unread_and_flagged(self) -> None:
        messages = inbox(4)
        messages[0].flags.add("\\Flagged")
        with fake_imap_server(mailboxes={"INBOX": messages}):
            with ImapClient(ACCOUNT) as client:
                self.assertEqual(client.search_uids("INBOX", "unread"), [1, 3])
                self.assertEqual(client.search_uids("INBOX", "flagged"), [1])

    def test_search_by_sender(self) -> None:
        messages = [
            FakeMessage(uid=1, raw=sample_message(sender="boss@corp.example")),
            FakeMessage(uid=2, raw=sample_message(sender="spam@bad.example")),
        ]
        with fake_imap_server(mailboxes={"INBOX": messages}):
            with ImapClient(ACCOUNT) as client:
                self.assertEqual(client.search_uids("INBOX", "boss@corp.example"), [1])

    def test_fetch_returns_byte_exact_payload(self) -> None:
        """The bytes must survive untouched.

        imbox >= 0.10 exposes ``raw_email`` as ``str_encode(raw, charset,
        errors="ignore")``; for this latin-1 body that silently drops the
        accented bytes.  We fetch with BODY.PEEK[] instead, so nothing is lost.
        """
        raw = sample_message(subject="Latin", body="caf\xe9 na\xefve", charset="iso-8859-1")
        self.assertIn(b"\xe9", raw)
        with fake_imap_server(mailboxes={"INBOX": [FakeMessage(uid=7, raw=raw)]}):
            with ImapClient(ACCOUNT) as client:
                messages = list(client.fetch("all"))
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].raw, raw)
        self.assertEqual(parse_email(messages[0].raw).text_body.strip(), "café naïve")

    def test_fetch_does_not_mark_messages_as_read(self) -> None:
        messages = inbox(2)
        for message in messages:
            message.flags.discard("\\Seen")
        with fake_imap_server(mailboxes={"INBOX": messages}) as server:
            with ImapClient(ACCOUNT) as client:
                list(client.fetch("all"))
            self.assertTrue(all("\\Seen" not in m.flags for m in server.mailboxes["INBOX"]))

    def test_fetch_limit_and_newest_first(self) -> None:
        with fake_imap_server(mailboxes={"INBOX": inbox(5)}):
            with ImapClient(ACCOUNT) as client:
                fetched = [int(m.uid) for m in client.fetch("all", limit=2)]
        self.assertEqual(fetched, [5, 4])

    def test_fetch_can_be_cancelled(self) -> None:
        with fake_imap_server(mailboxes={"INBOX": inbox(5)}):
            with ImapClient(ACCOUNT) as client:
                stop = {"value": False}
                collected = []
                for message in client.fetch("all", should_stop=lambda: stop["value"]):
                    collected.append(message)
                    stop["value"] = True
        self.assertEqual(len(collected), 1)

    def test_statuses_report_flags_and_size(self) -> None:
        messages = inbox(3)
        messages[0].flags.add("\\Flagged")
        with fake_imap_server(mailboxes={"INBOX": messages}):
            with ImapClient(ACCOUNT) as client:
                statuses = {s.uid: s for s in client.fetch_statuses("INBOX")}
        self.assertEqual(set(statuses), {1, 2, 3})
        self.assertTrue(statuses[1].flagged)
        self.assertFalse(statuses[1].seen)
        self.assertTrue(statuses[2].seen)
        self.assertGreater(statuses[1].size, 0)

    def test_server_side_search(self) -> None:
        messages = [
            FakeMessage(uid=1, raw=sample_message(subject="Invoice 42")),
            FakeMessage(uid=2, raw=sample_message(subject="Lunch")),
        ]
        with fake_imap_server(mailboxes={"INBOX": messages}):
            with ImapClient(ACCOUNT) as client:
                self.assertEqual(client.search_raw("INBOX", 'TEXT "Invoice"'), [1])


class FlagTests(unittest.TestCase):
    def test_add_and_remove_flags(self) -> None:
        messages = inbox(2)
        with fake_imap_server(mailboxes={"INBOX": messages}) as server:
            with ImapClient(ACCOUNT) as client:
                client.store_flags("INBOX", [1], ["\\Seen"], add=True)
                self.assertIn("\\Seen", server.mailboxes["INBOX"][0].flags)
                client.store_flags("INBOX", [1], ["\\Seen"], add=False)
                self.assertNotIn("\\Seen", server.mailboxes["INBOX"][0].flags)
                client.store_flags("INBOX", [1, 2], ["\\Flagged"], add=True)
                self.assertTrue(all("\\Flagged" in m.flags for m in server.mailboxes["INBOX"]))

    def test_mark_seen_keeps_the_original_api(self) -> None:
        with fake_imap_server(mailboxes={"INBOX": inbox(1)}) as server:
            with ImapClient(ACCOUNT) as client:
                client.select("INBOX")
                client.mark_seen(1)
        self.assertIn("\\Seen", server.mailboxes["INBOX"][0].flags)


class DeleteVerificationTests(unittest.TestCase):
    """A server that answers OK without deleting must not fool the client."""

    def test_delete_reports_messages_the_server_kept(self) -> None:
        with fake_imap_server(mailboxes={"INBOX": inbox(3), "Trash": []},
                              ignore_deletes=True) as server:
            with ImapClient(ACCOUNT) as client:
                ok, remaining = client.delete("INBOX", [1, 2], permanent=True)
        self.assertFalse(ok, "the delete silently failed and must be reported")
        self.assertEqual(remaining, [1, 2])
        self.assertEqual(len(server.mailboxes["INBOX"]), 3)

    def test_move_reports_messages_the_server_kept(self) -> None:
        with fake_imap_server(mailboxes={"INBOX": inbox(2), "Trash": []},
                              ignore_deletes=True) as server:
            with ImapClient(ACCOUNT) as client:
                ok, remaining = client.move("INBOX", [1], "Trash")
        self.assertFalse(ok)
        self.assertEqual(remaining, [1])
        self.assertEqual(len(server.mailboxes["INBOX"]), 2)

    def test_successful_delete_reports_nothing_remaining(self) -> None:
        with fake_imap_server(mailboxes={"INBOX": inbox(3), "Trash": []}):
            with ImapClient(ACCOUNT) as client:
                ok, remaining = client.delete("INBOX", [2], permanent=True)
        self.assertTrue(ok)
        self.assertEqual(remaining, [])

    def test_present_uids_answers_what_the_server_still_has(self) -> None:
        with fake_imap_server(mailboxes={"INBOX": inbox(3)}):
            with ImapClient(ACCOUNT) as client:
                self.assertEqual(client.present_uids("INBOX", [1, 2, 99]), {1, 2})


class DeleteAndMoveTests(unittest.TestCase):
    def test_move_uses_the_move_extension(self) -> None:
        with fake_imap_server(mailboxes={"INBOX": inbox(2), "Trash": []}) as server:
            with ImapClient(ACCOUNT) as client:
                ok, remaining = client.move("INBOX", [1], "Trash")
                self.assertTrue(ok)
                self.assertEqual(remaining, [])
        self.assertEqual(len(server.mailboxes["Trash"]), 1)
        self.assertEqual([m.uid for m in server.mailboxes["INBOX"]], [2])
        self.assertTrue(any(c[1] == "MOVE" for c in server.commands if c[0] == "uid"))

    def test_move_falls_back_to_copy_delete_without_the_extension(self) -> None:
        with fake_imap_server(mailboxes={"INBOX": inbox(2), "Trash": []},
                              capabilities=("IMAP4REV1",)) as server:
            with ImapClient(ACCOUNT) as client:
                ok, _ = client.move("INBOX", [1], "Trash")
                self.assertTrue(ok)
        self.assertEqual(len(server.mailboxes["Trash"]), 1)
        self.assertEqual([m.uid for m in server.mailboxes["INBOX"]], [2])
        commands = [c[1] for c in server.commands if c[0] == "uid"]
        self.assertIn("COPY", commands)

    def test_delete_moves_to_trash_by_default(self) -> None:
        with fake_imap_server(mailboxes={"INBOX": inbox(2), "Trash": []}) as server:
            with ImapClient(ACCOUNT) as client:
                client.delete("INBOX", [2], permanent=False, trash_folder="Trash")
        self.assertEqual(len(server.mailboxes["Trash"]), 1)
        self.assertEqual([m.uid for m in server.mailboxes["INBOX"]], [1])

    def test_permanent_delete_expunges(self) -> None:
        with fake_imap_server(mailboxes={"INBOX": inbox(3), "Trash": []}) as server:
            with ImapClient(ACCOUNT) as client:
                client.delete("INBOX", [2], permanent=True, trash_folder="Trash")
        self.assertEqual([m.uid for m in server.mailboxes["INBOX"]], [1, 3])
        self.assertEqual(server.mailboxes["Trash"], [])

    def test_append_stores_a_message(self) -> None:
        raw = sample_message(subject="Draft")
        with fake_imap_server(mailboxes={"INBOX": [], "Drafts": []}) as server:
            with ImapClient(ACCOUNT) as client:
                self.assertTrue(client.append("Drafts", raw, ["\\Draft"]))
        self.assertEqual(len(server.mailboxes["Drafts"]), 1)
        self.assertIn("\\Draft", server.mailboxes["Drafts"][0].flags)


class FolderTests(unittest.TestCase):
    def test_special_use_flags_are_detected(self) -> None:
        mailboxes = {"INBOX": inbox(1), "[Gmail]/Sent Mail": [], "[Gmail]/Trash": [],
                     "[Gmail]/Drafts": [], "Work": []}
        flags = {
            "[Gmail]/Sent Mail": "\\HasNoChildren \\Sent",
            "[Gmail]/Trash": "\\HasNoChildren \\Trash",
            "[Gmail]/Drafts": "\\HasNoChildren \\Drafts",
        }
        with fake_imap_server(mailboxes=mailboxes, folder_flags=flags):
            with ImapClient(ACCOUNT) as client:
                folders = {f.name: f for f in client.list_folders()}
        self.assertEqual(folders["INBOX"].kind, "inbox")
        self.assertEqual(folders["[Gmail]/Sent Mail"].kind, "sent")
        self.assertEqual(folders["[Gmail]/Trash"].kind, "trash")
        self.assertEqual(folders["[Gmail]/Drafts"].kind, "drafts")
        self.assertEqual(folders["Work"].kind, "other")
        self.assertEqual(folders["[Gmail]/Sent Mail"].display, "Sent Mail")

    def test_folder_kind_falls_back_to_names(self) -> None:
        mailboxes = {"INBOX": [], "Sent": [], "Junk": [], "Papierkorb": []}
        with fake_imap_server(mailboxes=mailboxes):
            with ImapClient(ACCOUNT) as client:
                folders = {f.name: f.kind for f in client.list_folders()}
        self.assertEqual(folders["Sent"], "sent")
        self.assertEqual(folders["Junk"], "spam")
        self.assertEqual(folders["Papierkorb"], "trash")

    def test_unread_counts(self) -> None:
        with fake_imap_server(mailboxes={"INBOX": inbox(4)}):
            with ImapClient(ACCOUNT) as client:
                folders = {f.name: f for f in client.list_folders(with_counts=True)}
        self.assertEqual(folders["INBOX"].total, 4)
        self.assertEqual(folders["INBOX"].unread, 2)

    def test_noselect_folders_are_marked(self) -> None:
        with fake_imap_server(mailboxes={"INBOX": [], "[Gmail]": []},
                              folder_flags={"[Gmail]": "\\Noselect \\HasChildren"}):
            with ImapClient(ACCOUNT) as client:
                folders = {f.name: f for f in client.list_folders()}
        self.assertFalse(folders["[Gmail]"].selectable)

    def test_utf7_folder_names(self) -> None:
        self.assertEqual(imap_utf7_decode("Entw&APw-rfe"), "Entwürfe")
        self.assertEqual(imap_utf7_decode("INBOX"), "INBOX")
        self.assertEqual(imap_utf7_encode("Entwürfe"), "Entw&APw-rfe")
        self.assertEqual(imap_utf7_decode(imap_utf7_encode("受信箱")), "受信箱")
        self.assertEqual(imap_utf7_decode("A&-B"), "A&B")

    def test_mailbox_quoting(self) -> None:
        self.assertEqual(quote_mailbox("INBOX"), "INBOX")
        self.assertEqual(quote_mailbox("[Gmail]/Sent Mail"), '"[Gmail]/Sent Mail"')
        # "&" is a legal atom character, so the UTF-7 form needs no quotes...
        self.assertEqual(quote_mailbox("Entwürfe"), "Entw&APw-rfe")
        # ...but a space in the decoded name still forces them.
        self.assertEqual(quote_mailbox("Gelöschte Objekte"), '"Gel&APY-schte Objekte"')

    def test_uid_validity(self) -> None:
        with fake_imap_server(mailboxes={"INBOX": inbox(1)}):
            with ImapClient(ACCOUNT) as client:
                self.assertEqual(client.uid_validity("INBOX"), 42)


if __name__ == "__main__":
    unittest.main(verbosity=2)
