"""Tests for composing and sending mail.

Every built message is parsed back with the application's own parser, so the
assertions describe what a *receiving* client would see.

    python tests/test_sender.py
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import SmtpSettings  # noqa: E402
from fake_smtp import fake_smtp_server  # noqa: E402
from mail_parser import parse_email  # noqa: E402
from mail_sender import (  # noqa: E402
    Draft,
    DraftAttachment,
    SendError,
    SmtpSender,
    build_forward,
    build_message,
    build_reply,
)

SMTP = SmtpSettings(host="smtp.example.com", port=587, security="starttls",
                    username="me@example.com", password="secret")

PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)


def simple_draft(**overrides) -> Draft:
    values = dict(
        to=["Alice <alice@example.com>"],
        subject="Hello",
        body_text="Hi there",
        from_address="me@example.com",
    )
    values.update(overrides)
    return Draft(**values)


class BuildMessageTests(unittest.TestCase):
    def test_plain_text_message(self) -> None:
        mail = parse_email(build_message(simple_draft()).as_bytes())
        self.assertEqual(mail.subject, "Hello")
        self.assertEqual(mail.to_addrs[0].email, "alice@example.com")
        self.assertEqual(mail.to_addrs[0].name, "Alice")
        self.assertEqual(mail.text_body.strip(), "Hi there")
        self.assertTrue(mail.message_id)

    def test_utf8_subject_and_display_name(self) -> None:
        draft = simple_draft(subject="Grüße – 日本語 – ✓",
                             from_name="Jörg Müller",
                             to=["Zoë <zoe@example.com>"])
        raw = build_message(draft).as_bytes()
        # The wire format must stay 7-bit clean in the headers.
        header_block = raw.split(b"\r\n\r\n", 1)[0]
        self.assertTrue(all(byte < 128 for byte in header_block))
        mail = parse_email(raw)
        self.assertEqual(mail.subject, "Grüße – 日本語 – ✓")
        self.assertEqual(mail.from_addrs[0].name, "Jörg Müller")
        self.assertEqual(mail.to_addrs[0].name, "Zoë")

    def test_multiple_recipients_cc_and_bcc(self) -> None:
        draft = simple_draft(
            to=["a@example.com", "B <b@example.com>"],
            cc=["c@example.com"],
            bcc=["secret@example.com"],
        )
        self.assertEqual(
            draft.recipients,
            ["a@example.com", "b@example.com", "c@example.com", "secret@example.com"],
        )
        mail = parse_email(build_message(draft).as_bytes())
        self.assertEqual([a.email for a in mail.to_addrs], ["a@example.com", "b@example.com"])
        self.assertEqual([a.email for a in mail.cc_addrs], ["c@example.com"])
        # Bcc must not travel with the message handed to the server.
        self.assertEqual(mail.bcc_addrs, [])

    def test_bcc_is_kept_for_the_sent_copy(self) -> None:
        draft = simple_draft(bcc=["secret@example.com"])
        mail = parse_email(build_message(draft, include_bcc=True).as_bytes())
        self.assertEqual([a.email for a in mail.bcc_addrs], ["secret@example.com"])

    def test_html_alternative(self) -> None:
        draft = simple_draft(body_text="plain version",
                             body_html="<p>rich <b>version</b></p>")
        mail = parse_email(build_message(draft).as_bytes())
        self.assertEqual(mail.text_body.strip(), "plain version")
        self.assertIn("rich <b>version</b>", mail.html_body)

    def test_html_only_draft_gets_a_text_alternative(self) -> None:
        draft = simple_draft(body_text="", body_html="<p>only rich</p>")
        mail = parse_email(build_message(draft).as_bytes())
        self.assertIn("only rich", mail.text_body)
        self.assertIn("only rich", mail.html_body)

    def test_attachments(self) -> None:
        draft = simple_draft(attachments=[
            DraftAttachment(filename="report.pdf", data=b"%PDF-1.4 data",
                            content_type="application/pdf"),
            DraftAttachment(filename="notes.txt", data="räksmörgås".encode(),
                            content_type="text/plain"),
        ])
        mail = parse_email(build_message(draft).as_bytes())
        names = [a.filename for a in mail.attachments]
        self.assertEqual(sorted(names), ["notes.txt", "report.pdf"])
        by_name = {a.filename: a for a in mail.attachments}
        self.assertEqual(by_name["report.pdf"].data, b"%PDF-1.4 data")
        self.assertEqual(by_name["notes.txt"].data.decode(), "räksmörgås")

    def test_inline_image_becomes_a_cid_part(self) -> None:
        draft = simple_draft(
            body_html='<p>look: <img src="cid:logo1"></p>',
            attachments=[DraftAttachment(filename="logo.png", data=PNG,
                                         content_type="image/png", content_id="logo1")],
        )
        mail = parse_email(build_message(draft).as_bytes())
        self.assertEqual([i.content_id for i in mail.inline_images], ["logo1"])
        self.assertEqual(mail.inline_images[0].data, PNG)
        self.assertEqual(mail.attachments, [])          # inline, not a file

    def test_inline_image_and_attachment_together(self) -> None:
        draft = simple_draft(
            body_html='<p><img src="cid:pic"></p>',
            attachments=[
                DraftAttachment(filename="pic.png", data=PNG, content_type="image/png",
                                content_id="pic"),
                DraftAttachment(filename="doc.pdf", data=b"%PDF", content_type="application/pdf"),
            ],
        )
        mail = parse_email(build_message(draft).as_bytes())
        self.assertEqual([a.filename for a in mail.attachments], ["doc.pdf"])
        self.assertEqual([i.content_id for i in mail.inline_images], ["pic"])
        self.assertIn("img", mail.html_body)

    def test_reply_to_and_high_priority(self) -> None:
        draft = simple_draft(reply_to="desk@example.com", high_priority=True)
        raw = build_message(draft).as_bytes()
        mail = parse_email(raw)
        self.assertEqual(mail.reply_to[0].email, "desk@example.com")
        headers = {name.lower(): value for name, value in mail.headers}
        self.assertIn("1", headers.get("x-priority", ""))
        self.assertEqual(headers.get("importance", "").lower(), "high")

    def test_threading_headers(self) -> None:
        draft = simple_draft(in_reply_to="<parent@example.com>",
                             references=["<root@example.com>", "<parent@example.com>"])
        mail = parse_email(build_message(draft).as_bytes())
        self.assertEqual(mail.in_reply_to, "<parent@example.com>")
        self.assertEqual(mail.references, ["<root@example.com>", "<parent@example.com>"])

    def test_validation_catches_missing_recipient(self) -> None:
        self.assertIn("no recipient", " ".join(Draft(from_address="me@x.com").validate()).lower())


class ReplyForwardTests(unittest.TestCase):
    def setUp(self) -> None:
        raw = build_message(Draft(
            to=["me@example.com", "colleague@example.com"],
            cc=["watcher@example.com"],
            subject="Project update",
            body_text="The numbers are in.",
            body_html="<p>The numbers are <b>in</b>.</p>",
            from_address="boss@example.com",
            from_name="The Boss",
            message_id="<original@example.com>",
        )).as_bytes()
        self.original = parse_email(raw, uid="5", folder="INBOX")
        self.raw = raw

    def test_reply_targets_the_sender_only(self) -> None:
        draft = build_reply(self.original, "me@example.com")
        self.assertEqual([a.split("<")[-1].strip(">") for a in draft.to], ["boss@example.com"])
        self.assertEqual(draft.cc, [])
        self.assertEqual(draft.subject, "Re: Project update")

    def test_reply_all_includes_others_but_not_me(self) -> None:
        draft = build_reply(self.original, "me@example.com", reply_all=True)
        everyone = " ".join(draft.to + draft.cc)
        self.assertIn("boss@example.com", everyone)
        self.assertIn("colleague@example.com", everyone)
        self.assertIn("watcher@example.com", everyone)
        self.assertNotIn("me@example.com", everyone)

    def test_reply_preserves_threading(self) -> None:
        draft = build_reply(self.original, "me@example.com")
        self.assertEqual(draft.in_reply_to, "<original@example.com>")
        self.assertIn("<original@example.com>", draft.references)
        sent = parse_email(build_message(draft).as_bytes())
        self.assertEqual(sent.in_reply_to, "<original@example.com>")

    def test_reply_quotes_the_original_in_both_formats(self) -> None:
        draft = build_reply(self.original, "me@example.com")
        self.assertIn("> The numbers are in.", draft.body_text)
        self.assertIn("wrote:", draft.body_text)
        self.assertIn("blockquote", draft.body_html)
        self.assertIn("The numbers are <b>in</b>", draft.body_html)

    def test_reply_does_not_carry_scripts_from_the_original(self) -> None:
        raw = build_message(Draft(
            to=["me@example.com"], subject="Nasty", from_address="bad@example.com",
            body_html="<p>hi</p><script>steal()</script>"
                      '<img src="https://tracker.example/p.gif" width="1" height="1">',
        )).as_bytes()
        draft = build_reply(parse_email(raw), "me@example.com")
        self.assertNotIn("steal()", draft.body_html)
        self.assertNotIn("tracker.example", draft.body_html)

    def test_subject_is_not_prefixed_twice(self) -> None:
        self.original.subject = "Re: Project update"
        self.assertEqual(build_reply(self.original, "me@x.com").subject, "Re: Project update")

    def test_forward_inline_keeps_headers_and_attachments(self) -> None:
        with_attachment = parse_email(build_message(Draft(
            to=["me@example.com"], subject="With file", from_address="boss@example.com",
            body_text="see attached",
            attachments=[DraftAttachment(filename="a.txt", data=b"content",
                                         content_type="text/plain")],
        )).as_bytes())
        draft = build_forward(with_attachment, "me@example.com")
        self.assertEqual(draft.subject, "Fwd: With file")
        self.assertIn("Forwarded message", draft.body_text)
        self.assertIn("boss@example.com", draft.body_text)
        self.assertEqual([a.filename for a in draft.attachments], ["a.txt"])
        sent = parse_email(build_message(draft).as_bytes())
        self.assertEqual([a.filename for a in sent.attachments], ["a.txt"])

    def test_forward_as_eml_attachment(self) -> None:
        draft = build_forward(self.original, "me@example.com",
                              as_attachment=True, raw_message=self.raw)
        self.assertEqual(len(draft.attachments), 1)
        self.assertTrue(draft.attachments[0].filename.endswith(".eml"))
        sent = parse_email(build_message(draft).as_bytes())
        self.assertEqual(len(sent.attachments), 1)
        self.assertEqual(sent.attachments[0].content_type, "message/rfc822")
        # The forwarded original must be readable inside the attachment.
        inner = parse_email(sent.attachments[0].data)
        self.assertEqual(inner.subject, "Project update")


class SendingTests(unittest.TestCase):
    def test_send_uses_starttls_login_and_sendmail(self) -> None:
        with fake_smtp_server() as fake:
            result = SmtpSender(SMTP).send(simple_draft(cc=["c@example.com"]))
        server = fake.instances[0]
        self.assertTrue(server.started_tls)
        self.assertEqual(server.logged_in_as, "me@example.com")
        self.assertTrue(server.quit_called)
        from_addr, recipients, raw = server.sent[0]
        self.assertEqual(from_addr, "me@example.com")
        self.assertEqual(recipients, ["alice@example.com", "c@example.com"])
        self.assertEqual(parse_email(raw).subject, "Hello")
        self.assertTrue(result.message_id)

    def test_ssl_mode_skips_starttls(self) -> None:
        settings = SmtpSettings(**{**SMTP.__dict__, "security": "ssl", "port": 465})
        with fake_smtp_server() as fake:
            SmtpSender(settings).send(simple_draft())
        self.assertFalse(fake.instances[0].started_tls)

    def test_authentication_failure_is_explained(self) -> None:
        settings = SmtpSettings(**{**SMTP.__dict__, "host": "smtp.gmail.com"})
        with fake_smtp_server(fail_on="login"):
            with self.assertRaises(SendError) as caught:
                SmtpSender(settings).send(simple_draft())
        self.assertIn("app password", str(caught.exception).lower())

    def test_all_recipients_refused(self) -> None:
        with fake_smtp_server(refuse_recipients=("alice@example.com",)):
            with self.assertRaises(SendError) as caught:
                SmtpSender(SMTP).send(simple_draft())
        self.assertIn("rejected these recipients", str(caught.exception))

    def test_partial_refusal_is_reported_but_not_fatal(self) -> None:
        with fake_smtp_server(refuse_recipients=("alice@example.com",)):
            result = SmtpSender(SMTP).send(simple_draft(to=["alice@example.com", "b@example.com"]))
        self.assertIn("alice@example.com", result.refused)

    def test_server_disconnect_is_explained(self) -> None:
        with fake_smtp_server(fail_on="ehlo"):
            with self.assertRaises(SendError) as caught:
                SmtpSender(SMTP).send(simple_draft())
        self.assertIn("dropped", str(caught.exception))

    def test_tls_error_is_explained(self) -> None:
        with fake_smtp_server(fail_on="starttls"):
            with self.assertRaises(SendError) as caught:
                SmtpSender(SMTP).send(simple_draft())
        self.assertIn("Secure connection", str(caught.exception))

    def test_missing_host_is_reported(self) -> None:
        with self.assertRaises(SendError):
            SmtpSender(SmtpSettings(host="", username="u", password="p")).send(simple_draft())

    def test_smtp_defaults_are_derived_from_imap(self) -> None:
        from config import AccountSettings

        resolved = SmtpSettings().resolved(
            AccountSettings(host="imap.gmail.com", username="u@gmail.com", password="pw")
        )
        self.assertEqual(resolved.host, "smtp.gmail.com")
        self.assertEqual(resolved.port, 587)
        self.assertEqual(resolved.security, "starttls")
        self.assertEqual(resolved.username, "u@gmail.com")


if __name__ == "__main__":
    unittest.main(verbosity=2)
