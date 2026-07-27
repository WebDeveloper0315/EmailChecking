"""Unit tests for the parsing / decoding / sanitising layers.

Run with:  python -m unittest discover -s tests -v
No network, no Qt, no credentials needed.
"""

from __future__ import annotations

import base64
import os
import sys
import unittest
from email.message import EmailMessage

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from attachment_manager import human_size, sanitize_filename  # noqa: E402
from html_processor import html_to_text, sanitize_html, text_to_html  # noqa: E402
from mail_parser import parse_email  # noqa: E402
from mime_decoder import decode_bytes, decode_header_value, normalize_charset, parse_date  # noqa: E402


def build(headers: dict[str, str], body: str = "", subtype: str = "plain") -> bytes:
    message = EmailMessage()
    for name, value in headers.items():
        message[name] = value
    message.set_content(body, subtype=subtype)
    return message.as_bytes()


class HeaderDecodingTests(unittest.TestCase):
    def test_rfc2047_base64_subject(self) -> None:
        encoded = "=?UTF-8?B?" + base64.b64encode("Grüße aus Köln".encode()).decode() + "?="
        self.assertEqual(decode_header_value(encoded), "Grüße aus Köln")

    def test_rfc2047_quoted_printable_and_mixed_parts(self) -> None:
        self.assertEqual(
            decode_header_value("=?iso-8859-1?Q?Caf=E9?= meeting"), "Café meeting"
        )

    def test_unknown_charset_in_encoded_word_does_not_crash(self) -> None:
        value = "=?bogus-charset-42?B?" + base64.b64encode(b"hello").decode() + "?="
        self.assertIn("hello", decode_header_value(value))

    def test_korean_ks_c_5601_alias(self) -> None:
        raw = "안녕하세요".encode("cp949")
        value = "=?ks_c_5601-1987?B?" + base64.b64encode(raw).decode() + "?="
        self.assertEqual(decode_header_value(value), "안녕하세요")

    def test_folded_header_is_unfolded(self) -> None:
        self.assertEqual(decode_header_value("first\r\n  second"), "first second")

    def test_addresses_are_decoded(self) -> None:
        encoded = "=?UTF-8?B?" + base64.b64encode("Jörg Müller".encode()).decode() + "?="
        raw = build({"From": f"{encoded} <jorg@example.com>", "To": "a@x.com, b@y.com"}, "hi")
        mail = parse_email(raw)
        self.assertEqual(mail.from_addrs[0].name, "Jörg Müller")
        self.assertEqual(mail.from_addrs[0].email, "jorg@example.com")
        self.assertEqual([a.email for a in mail.to_addrs], ["a@x.com", "b@y.com"])

    def test_date_parsing_and_broken_date(self) -> None:
        self.assertIsNotNone(parse_date("Sat, 25 Jul 2026 15:12:03 -0700"))
        self.assertIsNone(parse_date("yesterday afternoon"))


class CharsetTests(unittest.TestCase):
    def test_aliases(self) -> None:
        self.assertEqual(normalize_charset("UTF8"), "utf-8")
        self.assertEqual(normalize_charset("ks_c_5601-1987"), "cp949")
        self.assertEqual(normalize_charset("gb2312"), "gb18030")
        self.assertEqual(normalize_charset("Shift_JIS"), "cp932")
        self.assertEqual(normalize_charset(None), "utf-8")
        self.assertEqual(normalize_charset("totally-made-up"), "")

    def test_utf16_bom_wins_over_declared_charset(self) -> None:
        text, charset = decode_bytes("héllo".encode("utf-16"), "us-ascii")
        self.assertEqual(text, "héllo")
        self.assertIn("utf-16", charset)

    def test_wrong_declared_charset_falls_back(self) -> None:
        text, _ = decode_bytes("日本語テキスト".encode("shift_jis"), "us-ascii")
        self.assertTrue(text)  # never raises, and produces something

    def test_undecodable_bytes_never_raise(self) -> None:
        text, _ = decode_bytes(b"\xff\xfe\x00broken\x80\x81", "definitely-not-a-charset")
        self.assertIsInstance(text, str)

    def test_gbk_body_decodes(self) -> None:
        body = "中文测试内容"
        message = EmailMessage()
        message["Subject"] = "gbk"
        message["From"] = "a@b.c"
        message.set_content(body, charset="gbk")
        mail = parse_email(message.as_bytes())
        self.assertIn("中文测试", mail.text_body)


class StructureTests(unittest.TestCase):
    ALTERNATIVE = (
        b"From: Sender <s@example.com>\r\n"
        b"To: r@example.com\r\n"
        b"Subject: Alternative\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/alternative; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        b"plain version\r\n"
        b"--B\r\nContent-Type: text/html; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: quoted-printable\r\n\r\n"
        b"<p>html =C3=A4 version</p>\r\n"
        b"--B--\r\n"
    )

    def test_alternative_keeps_both_representations(self) -> None:
        mail = parse_email(self.ALTERNATIVE)
        self.assertEqual(mail.text_body.strip(), "plain version")
        self.assertIn("html ä version", mail.html_body)
        self.assertTrue(mail.has_html)

    def test_nested_mixed_related_alternative(self) -> None:
        png = base64.b64encode(b"\x89PNG\r\n\x1a\nfake-image-data").decode()
        raw = (
            b"From: a@b.c\r\nSubject: Nested\r\nMIME-Version: 1.0\r\n"
            b'Content-Type: multipart/mixed; boundary="OUT"\r\n\r\n'
            b"--OUT\r\n"
            b'Content-Type: multipart/related; boundary="REL"\r\n\r\n'
            b"--REL\r\n"
            b'Content-Type: multipart/alternative; boundary="ALT"\r\n\r\n'
            b"--ALT\r\nContent-Type: text/plain\r\n\r\ntext fallback\r\n"
            b"--ALT\r\nContent-Type: text/html\r\n\r\n"
            b'<p>hi <img src="cid:logo@x"></p>\r\n'
            b"--ALT--\r\n"
            b"--REL\r\nContent-Type: image/png\r\nContent-ID: <logo@x>\r\n"
            b"Content-Transfer-Encoding: base64\r\n"
            b'Content-Disposition: inline; filename="logo.png"\r\n\r\n'
            + png.encode() + b"\r\n"
            b"--REL--\r\n"
            b"--OUT\r\n"
            b'Content-Type: application/pdf; name="report.pdf"\r\n'
            b"Content-Transfer-Encoding: base64\r\n"
            b'Content-Disposition: attachment; filename="report.pdf"\r\n\r\n'
            + base64.b64encode(b"%PDF-1.4 fake pdf payload").decode().encode() + b"\r\n"
            b"--OUT--\r\n"
        )
        mail = parse_email(raw)
        self.assertIn("hi", mail.html_body)
        self.assertEqual(mail.text_body.strip(), "text fallback")
        self.assertEqual([a.filename for a in mail.attachments], ["report.pdf"])
        self.assertEqual(mail.attachments[0].content_type, "application/pdf")
        self.assertEqual(mail.attachments[0].data, b"%PDF-1.4 fake pdf payload")
        self.assertEqual([i.content_id for i in mail.inline_images], ["logo@x"])
        self.assertTrue(mail.inline_images[0].data.startswith(b"\x89PNG"))

    def test_attachment_size_is_estimated_without_decoding(self) -> None:
        payload = os.urandom(5000)
        raw = (
            b"From: a@b.c\r\nSubject: big\r\nMIME-Version: 1.0\r\n"
            b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
            b"--B\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
            b"--B\r\nContent-Type: application/octet-stream\r\n"
            b"Content-Transfer-Encoding: base64\r\n"
            b'Content-Disposition: attachment; filename="blob.bin"\r\n\r\n'
            + base64.encodebytes(payload).replace(b"\n", b"\r\n") + b"\r\n"
            b"--B--\r\n"
        )
        mail = parse_email(raw)
        attachment = mail.attachments[0]
        self.assertEqual(attachment.size, len(payload))     # exact, without decoding
        self.assertIsNone(attachment._data)                 # nothing decoded yet
        self.assertEqual(attachment.data, payload)          # decodes on demand

    def test_plain_text_only_message(self) -> None:
        mail = parse_email(build({"From": "a@b.c", "Subject": "plain"}, "just text"))
        self.assertEqual(mail.text_body.strip(), "just text")
        self.assertFalse(mail.has_html)

    def test_attachment_without_filename_gets_one(self) -> None:
        raw = (
            b"From: a@b.c\r\nSubject: noname\r\nMIME-Version: 1.0\r\n"
            b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
            b"--B\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
            b"--B\r\nContent-Type: image/jpeg\r\n"
            b"Content-Transfer-Encoding: base64\r\n"
            b"Content-Disposition: attachment\r\n\r\n"
            + base64.b64encode(b"\xff\xd8\xff-fake-jpeg").decode().encode() + b"\r\n"
            b"--B--\r\n"
        )
        mail = parse_email(raw)
        self.assertEqual(len(mail.attachments), 1)
        self.assertTrue(mail.attachments[0].filename.endswith((".jpg", ".jpe", ".jpeg")))

    def test_forwarded_message_becomes_an_eml_attachment(self) -> None:
        inner = build({"From": "x@y.z", "Subject": "Inner subject"}, "inner body")
        raw = (
            b"From: a@b.c\r\nSubject: Fwd\r\nMIME-Version: 1.0\r\n"
            b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
            b"--B\r\nContent-Type: text/plain\r\n\r\nsee attached\r\n"
            b"--B\r\nContent-Type: message/rfc822\r\n\r\n" + inner + b"\r\n"
            b"--B--\r\n"
        )
        mail = parse_email(raw)
        self.assertEqual(mail.text_body.strip(), "see attached")
        self.assertEqual(len(mail.attachments), 1)
        self.assertTrue(mail.attachments[0].filename.endswith(".eml"))
        self.assertIn(b"inner body", mail.attachments[0].data)

    def test_base64_encoded_message_rfc822_is_still_readable(self) -> None:
        """Some clients base64 a message/rfc822 part, which RFC 2046 forbids."""
        inner = build({"From": "x@y.z", "Subject": "Encoded inner"}, "inner body")
        raw = (
            b"From: a@b.c\r\nSubject: Fwd\r\nMIME-Version: 1.0\r\n"
            b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
            b"--B\r\nContent-Type: text/plain\r\n\r\nsee attached\r\n"
            b"--B\r\nContent-Type: message/rfc822\r\n"
            b"Content-Transfer-Encoding: base64\r\n"
            b'Content-Disposition: attachment; filename="inner.eml"\r\n\r\n'
            + base64.encodebytes(inner).replace(b"\n", b"\r\n") + b"\r\n"
            b"--B--\r\n"
        )
        mail = parse_email(raw)
        self.assertEqual(len(mail.attachments), 1)
        self.assertIn(b"inner body", mail.attachments[0].data)
        self.assertEqual(parse_email(mail.attachments[0].data).subject, "Encoded inner")


class RobustnessTests(unittest.TestCase):
    def test_missing_headers(self) -> None:
        mail = parse_email(b"\r\nbody without any headers\r\n")
        self.assertEqual(mail.display_subject, "(no subject)")
        self.assertEqual(mail.sender, "(unknown sender)")
        self.assertIn("body without any headers", mail.text_body)

    def test_broken_boundary_does_not_raise(self) -> None:
        raw = (
            b"From: a@b.c\r\nSubject: broken\r\nMIME-Version: 1.0\r\n"
            b'Content-Type: multipart/mixed; boundary="NOPE"\r\n\r\n'
            b"--WRONG\r\nContent-Type: text/plain\r\n\r\norphan text\r\n"
        )
        mail = parse_email(raw)
        # The stranded text must still be shown, not offered as a nameless
        # "attachment" of type multipart/mixed.
        self.assertIn("orphan text", mail.text_body)
        self.assertEqual(mail.attachments, [])
        self.assertTrue(any("boundary" in w for w in mail.warnings))

    def test_garbage_input(self) -> None:
        mail = parse_email(os.urandom(2048))
        self.assertIsInstance(mail.warnings, list)

    def test_empty_input(self) -> None:
        mail = parse_email(b"")
        self.assertEqual(mail.raw_size, 0)
        self.assertTrue(mail.warnings)

    def test_corrupt_base64_body(self) -> None:
        raw = (
            b"From: a@b.c\r\nSubject: bad base64\r\nMIME-Version: 1.0\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"Content-Transfer-Encoding: base64\r\n\r\n"
            b"!!!not-base64-at-all!!!\r\n"
        )
        mail = parse_email(raw)
        self.assertIsInstance(mail.text_body, str)

    def test_str_input_is_accepted(self) -> None:
        mail = parse_email("From: a@b.c\r\nSubject: as text\r\n\r\nhello")
        self.assertEqual(mail.subject, "as text")


class HtmlProcessingTests(unittest.TestCase):
    def test_void_meta_does_not_swallow_the_message(self) -> None:
        """Regression: <meta> has no closing tag.

        Treating it like <script> (drop everything until </meta>) discarded the
        rest of the body - real Japanese newsletters put <meta> after <head>,
        and the whole message rendered blank.
        """
        result = sanitize_html(
            "<html><head><title>t</title></head><body>"
            '<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">'
            "<p>本日のおススメ案件</p>"
            '<link rel="stylesheet" href="https://example.com/mail.css">'
            "<p>after the link</p></body></html>"
        )
        self.assertIn("本日のおススメ案件", result.html)
        self.assertIn("after the link", result.html)
        self.assertNotIn("stylesheet", result.html)

    def test_html_to_text_survives_void_drop_tags(self) -> None:
        text = html_to_text('<meta charset="utf-8"><p>visible</p>'
                            '<link rel="x"><p>also visible</p>')
        self.assertIn("visible", text)
        self.assertIn("also visible", text)

    def test_other_void_drop_tags_behave(self) -> None:
        for tag in ('<base href="https://x.example/">', '<input type="text">',
                    '<source src="a.mp4">', '<embed src="a.swf">'):
            result = sanitize_html(f"<div>before{tag}after</div>")
            self.assertIn("before", result.html, tag)
            self.assertIn("after", result.html, tag)

    def test_text_hidden_by_an_unknown_wrapper_is_recovered(self) -> None:
        """The safety net: markup that yields no text falls back to the text."""
        result = sanitize_html("<form><p>only inside a dropped element</p></form>")
        self.assertIn("only inside a dropped element", html_to_text(result.html))

    def test_text_inside_forms_and_noscript_is_kept(self) -> None:
        """These wrap visible wording in marketing mail; only the controls go."""
        result = sanitize_html(
            "<form action='https://x.example'><p>Unsubscribe from this list</p>"
            "<input type='email' value='secret@example.com'>"
            "<button>Send</button></form>"
            "<noscript><p>Enable images to see this</p></noscript>"
        )
        self.assertIn("Unsubscribe from this list", result.html)
        self.assertIn("Send", result.html)
        self.assertIn("Enable images to see this", result.html)
        self.assertNotIn("<input", result.html)
        self.assertNotIn("secret@example.com", result.html)
        self.assertNotIn("<form", result.html)

    def test_select_and_textarea_are_still_dropped_entirely(self) -> None:
        result = sanitize_html("<select><option>hidden choice</option></select>"
                               "<textarea>hidden text</textarea><p>kept</p>")
        self.assertNotIn("hidden choice", result.html)
        self.assertNotIn("hidden text", result.html)
        self.assertIn("kept", result.html)

    def test_unclosed_style_swallows_the_rest_like_a_browser(self) -> None:
        """Documented behaviour, not an accident.

        HTML says everything after an unterminated <style> is CSS, and browsers
        agree, so the sanitiser cannot invent text here.  The viewer covers this
        by showing the message's plain-text part instead (see the GUI tests).
        """
        result = sanitize_html("<div><style>p{color:red}<p>swallowed</div>")
        self.assertNotIn("color:red", result.html)
        self.assertEqual(html_to_text(result.html).strip(), "")

    def test_script_and_style_are_removed_with_content(self) -> None:
        result = sanitize_html(
            "<p>ok</p><script>alert('x')</script><style>p{color:red}</style>"
        )
        self.assertIn("<p>ok</p>", result.html)
        self.assertNotIn("alert", result.html)
        self.assertNotIn("color:red", result.html)

    def test_event_handlers_and_javascript_urls_are_stripped(self) -> None:
        result = sanitize_html(
            '<a href="javascript:evil()" onclick="evil()">x</a>'
            '<a href="https://ok.example">good</a>'
        )
        self.assertNotIn("javascript:", result.html)
        self.assertNotIn("onclick", result.html)
        self.assertIn("https://ok.example", result.html)

    def test_tracking_pixel_removed(self) -> None:
        result = sanitize_html(
            '<img src="https://t.example/pixel.gif" width="1" height="1">'
            '<p>body</p>',
            allow_remote_images=True,
        )
        self.assertEqual(result.trackers_removed, 1)
        self.assertNotIn("pixel.gif", result.html)

    def test_remote_images_blocked_by_default(self) -> None:
        blocked = sanitize_html('<img src="https://cdn.example/a.png" alt="pic">')
        self.assertEqual(blocked.remote_images_blocked, 1)
        self.assertNotIn("cdn.example", blocked.html)

        allowed = sanitize_html(
            '<img src="https://cdn.example/a.png" alt="pic">', allow_remote_images=True
        )
        self.assertIn("cdn.example", allowed.html)
        self.assertEqual(allowed.remote_images_shown, 1)

    def test_cid_images_are_resolved_and_reported_when_missing(self) -> None:
        resolved = sanitize_html(
            '<img src="cid:logo@x">', cid_resolver=lambda cid: "data:image/png;base64,AAAA"
        )
        self.assertIn("data:image/png;base64,AAAA", resolved.html)

        missing = sanitize_html('<img src="cid:gone@x">', cid_resolver=lambda cid: None)
        self.assertEqual(missing.missing_inline_images, ["gone@x"])
        self.assertIn("inline image", missing.html)

    def test_tables_and_links_are_preserved(self) -> None:
        result = sanitize_html(
            '<table border="1"><tr><td colspan="2">cell</td></tr></table>'
            '<a href="https://example.com/x?a=1&amp;b=2">link</a>'
        )
        self.assertIn("<table", result.html)
        self.assertIn('colspan="2"', result.html)
        self.assertIn("https://example.com/x?a=1&amp;b=2", result.html)

    def test_unclosed_tags_are_balanced(self) -> None:
        result = sanitize_html("<div><p>one<p>two<b>bold")
        self.assertEqual(result.html.count("<div"), 1)
        self.assertTrue(result.html.rstrip().endswith("</div>"))

    def test_dangerous_css_is_dropped(self) -> None:
        result = sanitize_html(
            '<div style="color:red; position:fixed; '
            'background:url(https://t.example/x.png)">text</div>'
        )
        self.assertIn("color:red", result.html)
        self.assertNotIn("position:fixed", result.html)
        self.assertNotIn("t.example", result.html)

    def test_entities_are_decoded_once(self) -> None:
        self.assertEqual(html_to_text("<p>caf&eacute; &amp; bar</p>"), "café & bar")

    def test_unknown_tags_are_unwrapped_but_text_kept(self) -> None:
        result = sanitize_html("<o:p>outlook text</o:p>")
        self.assertIn("outlook text", result.html)
        self.assertNotIn("<o:p>", result.html)

    def test_plain_text_rendering_links_and_quotes(self) -> None:
        html = text_to_html("see https://example.com now\n> quoted line")
        self.assertIn('href="https://example.com"', html)
        self.assertIn("quote", html)

    def test_urls_without_a_scheme_become_links(self) -> None:
        html = text_to_html("jobs at crowdworks.jp/public/jobs/search?order=new today")
        self.assertIn('href="https://crowdworks.jp/public/jobs/search?order=new"', html)
        self.assertIn(">crowdworks.jp/public/jobs/search?order=new</a>", html)

    def test_www_and_bare_domains(self) -> None:
        html = text_to_html("visit www.example.com or example.co.uk/page")
        self.assertIn('href="https://www.example.com"', html)
        self.assertIn('href="https://example.co.uk/page"', html)

    def test_ordinary_text_is_not_turned_into_links(self) -> None:
        html = text_to_html("version 1.2.3 released, e.g. today. Mr. Smith agreed.")
        self.assertNotIn("<a href", html)

    def test_trailing_punctuation_stays_outside_the_link(self) -> None:
        html = text_to_html("see https://example.com/page, then stop.")
        self.assertIn('href="https://example.com/page"', html)
        self.assertIn("</a>,", html)

    def test_bare_urls_in_html_bodies_become_clickable(self) -> None:
        result = sanitize_html("<p>Apply at crowdworks.jp/public/jobs now</p>")
        self.assertIn('href="https://crowdworks.jp/public/jobs"', result.html)

    def test_links_are_not_nested_inside_existing_anchors(self) -> None:
        result = sanitize_html('<a href="https://tracker.example/r?u=1">example.com/page</a>',
                               allow_remote_images=True)
        self.assertEqual(result.html.count("<a "), 1)
        self.assertIn("https://tracker.example/r?u=1", result.html)

    def test_links_carry_target_and_noopener(self) -> None:
        result = sanitize_html('<a href="https://example.com">click</a>')
        self.assertIn('target="_blank"', result.html)
        self.assertIn('rel="noopener noreferrer"', result.html)

    def test_html_to_text_keeps_structure(self) -> None:
        text = html_to_text("<p>one</p><ul><li>a</li><li>b</li></ul>")
        self.assertIn("one", text)
        self.assertIn("- a", text)


class AttachmentHelperTests(unittest.TestCase):
    def test_filename_sanitising(self) -> None:
        self.assertEqual(sanitize_filename("../../etc/passwd"), "passwd")
        self.assertEqual(sanitize_filename("bad:name?.txt"), "bad_name_.txt")
        self.assertEqual(sanitize_filename(""), "attachment.bin")
        self.assertTrue(sanitize_filename("con.txt").startswith("_"))
        self.assertLessEqual(len(sanitize_filename("x" * 500 + ".txt")), 205)

    def test_human_size(self) -> None:
        self.assertEqual(human_size(512), "512 B")
        self.assertEqual(human_size(1536), "1.5 KB")
        self.assertEqual(human_size(5 * 1024 * 1024), "5.0 MB")


if __name__ == "__main__":
    unittest.main(verbosity=2)
