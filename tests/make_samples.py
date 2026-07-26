"""Write realistic ``.eml`` files to ``samples/`` for manual testing.

    python tests/make_samples.py
    python main.py --eml-dir samples

The set covers what the viewer has to survive: multipart/alternative,
multipart/related with a CID image, attachments, RFC 2047 headers, GBK and
Shift-JIS bodies, a tracking pixel, a remote image, HTML-only mail, plain-text
only mail and a message with a broken MIME boundary.
"""

from __future__ import annotations

import base64
import os
import struct
import sys
import zlib
from pathlib import Path

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def png_bytes(width: int = 120, height: int = 60, rgb: tuple[int, int, int] = (26, 115, 232)) -> bytes:
    """A tiny valid PNG, generated so the repository stays binary-free."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def write(name: str, content: bytes) -> Path:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    path = SAMPLES / name
    path.write_bytes(content)
    print(f"wrote {path}  ({len(content)} bytes)")
    return path


def b64(data: bytes) -> bytes:
    return base64.encodebytes(data).replace(b"\n", b"\r\n").strip()


def newsletter() -> bytes:
    """The full monty: mixed > related > alternative, CID image, PDF, tracker."""
    logo = b64(png_bytes())
    pdf = b64(b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")
    subject = "=?UTF-8?B?" + base64.b64encode(
        "Your weekly digest – 週刊ダイジェスト".encode()).decode() + "?="
    return (
        f"From: =?UTF-8?Q?Acme_Ne=C3=BCs?= <news@acme.example>\r\n"
        f"To: Reader <reader@example.com>\r\n"
        f"Cc: archive@example.com\r\n"
        f"Reply-To: no-reply@acme.example\r\n"
        f"Subject: {subject}\r\n"
        "Date: Fri, 24 Jul 2026 09:15:00 +0200\r\n"
        "Message-ID: <digest-2026-07-24@acme.example>\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/mixed; boundary="MIXED"\r\n'
        "\r\n"
        "--MIXED\r\n"
        'Content-Type: multipart/related; boundary="RELATED"\r\n'
        "\r\n"
        "--RELATED\r\n"
        'Content-Type: multipart/alternative; boundary="ALT"\r\n'
        "\r\n"
        "--ALT\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "Content-Transfer-Encoding: quoted-printable\r\n"
        "\r\n"
        "Weekly digest\r\n"
        "=3D=3D=3D=3D=3D=3D=3D=3D=3D\r\n"
        "\r\n"
        "Hello Reader,\r\n"
        "\r\n"
        "Read the full story at https://acme.example/story/42\r\n"
        "\r\n"
        "> a quoted line from last week\r\n"
        "\r\n"
        "-- \r\n"
        "The Acme team\r\n"
        "--ALT\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        "Content-Transfer-Encoding: quoted-printable\r\n"
        "\r\n"
        "<html><head><style>.x{color:red}</style>"
        "<script>alert('this must never run')</script></head>"
        "<body style=3D\"font-family:Arial\">"
        "<img src=3D\"cid:logo@acme.example\" alt=3D\"Acme\" width=3D\"120\">"
        "<h1>Weekly digest</h1>"
        "<p>Hello <b>Reader</b>, here is what happened =E2=80=93 in <i>brief</i>.</p>"
        "<table border=3D\"1\" cellpadding=3D\"6\">"
        "<tr><th>Topic</th><th>Reads</th></tr>"
        "<tr><td>Release 2.0</td><td>1,204</td></tr>"
        "<tr><td>Roadmap</td><td>877</td></tr></table>"
        "<p><a href=3D\"https://acme.example/story/42\">Read the full story</a> "
        "or <a href=3D\"javascript:steal()\">this dangerous link</a>.</p>"
        "<blockquote>a quoted line from last week</blockquote>"
        "<img src=3D\"https://tracker.example/open?id=3D42\" width=3D\"1\" height=3D\"1\">"
        "<img src=3D\"https://cdn.example/banner.png\" width=3D\"400\" alt=3D\"Banner\">"
        "</body></html>\r\n"
        "--ALT--\r\n"
        "\r\n"
        "--RELATED\r\n"
        "Content-Type: image/png\r\n"
        "Content-Transfer-Encoding: base64\r\n"
        "Content-ID: <logo@acme.example>\r\n"
        'Content-Disposition: inline; filename="logo.png"\r\n'
        "\r\n"
    ).encode() + logo + (
        "\r\n"
        "--RELATED--\r\n"
        "\r\n"
        "--MIXED\r\n"
        "Content-Type: application/pdf\r\n"
        "Content-Transfer-Encoding: base64\r\n"
        'Content-Disposition: attachment; filename="=?UTF-8?Q?Bericht_M=C3=A4rz.pdf?="\r\n'
        "\r\n"
    ).encode() + pdf + b"\r\n--MIXED--\r\n"


def gbk_mail() -> bytes:
    body = "您好，\r\n\r\n这是一封使用 GBK 编码的测试邮件。\r\n\r\n谢谢！".encode("gbk")
    subject = "=?gb2312?B?" + base64.b64encode("测试邮件".encode("gbk")).decode() + "?="
    return (
        f"From: =?gb2312?B?{base64.b64encode('张三'.encode('gbk')).decode()}?= <zhang@example.cn>\r\n"
        "To: reader@example.com\r\n"
        f"Subject: {subject}\r\n"
        "Date: Thu, 23 Jul 2026 11:00:00 +0800\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/plain; charset=gb2312\r\n"
        "Content-Transfer-Encoding: base64\r\n"
        "\r\n"
    ).encode() + b64(body) + b"\r\n"


def lying_charset_mail() -> bytes:
    """Declares us-ascii but sends Shift-JIS - very common from old clients."""
    body = "件名のテスト\r\n\r\n本文は Shift-JIS です。".encode("shift_jis")
    return (
        "From: Tanaka <tanaka@example.jp>\r\n"
        "To: reader@example.com\r\n"
        "Subject: Shift-JIS body declared as us-ascii\r\n"
        "Date: Wed, 22 Jul 2026 18:30:00 +0900\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/plain; charset=us-ascii\r\n"
        "\r\n"
    ).encode() + body + b"\r\n"


def plain_only() -> bytes:
    """Shaped like the message in the original notebook."""
    return (
        "From: Microsoft account team <account-security-noreply@accountprotection.microsoft.com>\r\n"
        "To: blackghost1503@gmail.com\r\n"
        "Subject: Your single-use code\r\n"
        "Date: Sat, 25 Jul 2026 15:12:03 -0700\r\n"
        "Message-ID: <single-use-code@accountprotection.microsoft.com>\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        "Hi blackghost1503@gmail.com,\r\n"
        "\r\n"
        "We received your request for a single-use code to use with your Microsoft account.\r\n"
        "\r\n"
        "Your single-use code is: 258798\r\n"
        "\r\n"
        "Only enter this code on an official website or app.\r\n"
        "\r\n"
        "Thanks,\r\n"
        "The Microsoft account team\r\n"
        "Privacy Statement: https://go.microsoft.com/fwlink/?LinkId=521839\r\n"
    ).encode()


def html_only_broken_markup() -> bytes:
    """HTML-only, unclosed tags, Outlook noise, no plain-text alternative."""
    return (
        "From: Sales <sales@example.com>\r\n"
        "To: reader@example.com\r\n"
        "Subject: =?UTF-8?Q?Quote_=E2=80=93_no_plain_text_part?=\r\n"
        "Date: Tue, 21 Jul 2026 08:00:00 -0400\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/html; charset=iso-8859-1\r\n"
        "\r\n"
        "<div><o:p>Dear customer,<p>Our quote is <b>attached below"
        "<ul><li>Item A &ndash; 100 &euro;<li>Item B &ndash; 250 &euro;</ul>"
        "<p style='position:fixed;color:#c00'>This CSS must be neutralised."
        "<p>Caf\xe9 discount applies.</div>"
    ).encode("iso-8859-1")


def broken_mime() -> bytes:
    """Boundary that never appears - the parser must not give up."""
    return (
        "From: Broken Sender\r\n"
        "Subject: =?UTF-8?B?" + base64.b64encode("Broken MIME structure".encode()).decode() + "?=\r\n"
        "Date: not a real date\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/mixed; boundary="DOES-NOT-EXIST"\r\n'
        "\r\n"
        "--SOMETHING-ELSE\r\n"
        "Content-Type: text/plain\r\n"
        "\r\n"
        "This text is stranded outside any valid part, but should still show up.\r\n"
    ).encode()


def main() -> int:
    write("01-newsletter-multipart.eml", newsletter())
    write("02-gbk-chinese.eml", gbk_mail())
    write("03-shift-jis-wrong-charset.eml", lying_charset_mail())
    write("04-plain-text-only.eml", plain_only())
    write("05-html-only-broken-markup.eml", html_only_broken_markup())
    write("06-broken-mime.eml", broken_mime())
    print(f"\nOpen them with:  python main.py --eml-dir {SAMPLES}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    raise SystemExit(main())
