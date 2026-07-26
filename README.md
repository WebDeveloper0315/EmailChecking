# Mail Viewer

A desktop e-mail reader built **on top of** the existing `email-check.ipynb`
retrieval code. The notebook's imbox-based download logic is unchanged; this
project adds a proper MIME parser, an HTML sanitiser and a PySide6 viewer that
displays messages the way Gmail or Outlook does.

---

## 1. Quick start

```bash
pip install -r requirements.txt

python tests/make_samples.py          # write example .eml files
python main.py --eml-dir samples      # view them - no account needed

python main.py                        # the real thing: click "Account…", then "Fetch mail"
```

Other entry points:

```bash
python main.py --dump unread          # print messages as text, no GUI
python main.py --eml message.eml      # open specific files
python main.py --debug                # verbose logging
python tests/test_parser.py           # run the test suite
```

Credentials come from the login dialog, from `config.ini`, or from the
environment (`MAIL_USERNAME`, `MAIL_PASSWORD`, `IMAP_SERVER`, `IMAP_PORT`).
Gmail and Outlook require an **app password**, not the account password.

---

## 2. Design

### How it fits the existing script

The notebook does three things: connect with `Imbox(...)`, select messages with
one of four queries (`today` / `unread` / `all` / sender), and print
`message.body['plain']`. Only the third part is a problem - it prints Python
list reprs of half-decoded text and ignores HTML, attachments and charsets.

So `mail_receiver.py` keeps the first two parts verbatim and changes what comes
out of them: imbox already exposes the untouched RFC 5322 bytes of every message
it yields (`message.raw_email`), so the viewer can run its own parser over the
original bytes. No retrieval logic was rewritten. If `raw_email` is ever
missing, the receiver re-fetches that single message with `BODY.PEEK[]` through
imbox's own connection - the standard IMAP command that does not set `\Seen`.

### Pipeline

```
IMAP (imbox, unchanged)  ->  raw bytes
        |
        v
mail_parser.MailParser   ->  models.Email          walk the MIME tree
        |                                          (RFC 2046 body selection)
        +-- mime_decoder                           charsets, base64/QP, RFC 2047
        |
        v
html_processor.sanitize_html  ->  safe HTML        whitelist parser
        |
        v
viewer.BodyView (QtWebEngine, else QTextBrowser)
```

### Decisions worth explaining

| Decision | Why |
|---|---|
| `email.policy.compat32`, not `policy.default` | `default` pre-decodes headers but is strict about malformed input, which real mailboxes are full of. `compat32` hands us the raw strings; `mime_decoder` decodes them with a fallback at every step. |
| Own HTML sanitiser on `html.parser` | `bleach` is deprecated, `lxml` is a heavy binary dependency. `html.parser` is lenient with broken markup, streams, and is in the standard library. The whitelist is ~60 lines and testable. |
| Lazy attachment payloads | `Attachment.data` decodes on first access and caches; sizes are computed from the *encoded* length (exact for base64). A 20 MB mail can be listed and previewed without decoding a byte. |
| Remote images blocked by default | Remote images are the standard read-tracking mechanism. A banner offers "Show images" per message. Tracking pixels (1×1, hidden, known beacon URLs) are dropped even when images are enabled. |
| Inline images as `data:` URIs (QtWebEngine) or `loadResource` (QTextBrowser) | Both keep the message self-contained; no temporary files, no network. |
| `multipart/alternative` keeps *both* parts | The HTML part is displayed, the text part stays available for search, previews and mails where HTML fails to render. RFC 2046 says later parts are richer, so the last one wins. |
| `message/rfc822` becomes a saveable `.eml` attachment | Merging a forwarded message into the body would mix two senders' content, which is how phishing hides. |
| Worker thread for fetch + parse | Parsing a large message can take seconds; the UI thread only receives finished `Email` objects. |
| Off-the-record QtWebEngine profile, JavaScript disabled | Mail is untrusted content: no scripts, no cookies on disk, no local file access, links open in the real browser. |

### Problems in the original script (not changed, as agreed)

These are worth knowing, but none of them affect retrieval, so the code was left
alone:

1. **Credentials are hard-coded** in the notebook, including a real app
   password. Anyone with the file has the mailbox. The viewer reads credentials
   from a dialog, `config.ini` or the environment instead.
2. `print(str(message.body['plain']))` prints a **list repr** (`["Hi\r\n..."]`),
   which is why the notebook output shows literal `\r\n`. The `.replace()` calls
   cannot fix that because they operate on the repr.
3. **`mark_seen(uid)` runs unconditionally**, so reading mail changes server
   state. In the viewer that is opt-in (`Account…` → "Mark messages as read").
4. **`except:` with `traceback.print_exc()`** swallows `KeyboardInterrupt` and
   `SystemExit`, and the attachment loop hides the failing file name.
5. Attachments are written with the **sender-supplied file name**, so a name
   like `../../evil.txt` escapes the download folder. `attachment_manager.
   sanitize_filename` strips paths, control characters and Windows device names.
6. `delete_all_inbox_msgs()` deletes an entire mailbox with no confirmation -
   deliberately not wired into the UI.

---

## 3. File structure

```
EmailChecking/
├── main.py                  entry point, CLI args, logging, --dump mode
├── viewer.py                PySide6 UI (list, headers, body, attachments)
├── qt_bootstrap.py          fixes PySide6 DLL loading on Windows/conda
├── mail_receiver.py         imbox integration (unchanged retrieval) + .eml files
├── mail_parser.py           raw bytes -> models.Email (MIME tree walk)
├── mime_decoder.py          charsets, transfer encodings, RFC 2047, dates
├── html_processor.py        HTML sanitiser, text->HTML, HTML->text, CSS
├── attachment_manager.py    saving, safe file names, data: URIs for inline images
├── models.py                Address, Attachment, Email dataclasses
├── config.py                config.ini + environment settings
├── requirements.txt
├── tests/
│   ├── test_parser.py       38 unit tests, no network/Qt/credentials needed
│   └── make_samples.py      generates samples/*.eml
└── email-check.ipynb        the original script (untouched)
```

---

## 4. What the viewer handles

**Structure** `multipart/mixed`, `/alternative`, `/related`, `/digest`, nested
combinations, `message/rfc822`, single-part text or HTML, and messages whose
boundary never appears (the stranded text is shown with a warning instead of
being offered as a nameless attachment).

**Encodings** base64, quoted-printable, 7bit/8bit/binary; UTF-8/16/32 with BOM
detection, ISO-8859-*, cp1252, GB2312/GBK→GB18030, Big5, EUC-KR and
`ks_c_5601-1987`→cp949, Shift-JIS→cp932, EUC-JP, ISO-2022-JP. A wrong or
unknown charset falls back to detection (`charset-normalizer`/`chardet` when
installed), then to a candidate list, then to latin-1 with replacement - so
decoding never raises.

**Headers** RFC 2047 encoded words (`=?UTF-8?B?…?=`) in any header, mixed
encodings in one line, folded headers, multiple `Cc` headers, malformed dates.
All headers are viewable with "Show all headers".

**HTML** scripts/styles/iframes/forms/SVG removed with their content, event
handlers and `javascript:` URLs stripped, dangerous CSS (`expression()`,
`position:fixed`, remote `url()`) filtered, entities decoded once, unclosed tags
balanced, tables/links/formatting preserved, `cid:` images inlined.

**Attachments** name (RFC 2047 decoded), exact size, content type, save one or
all, image preview. Inline images are listed separately and shown in the body;
missing ones become a visible placeholder.

**Errors** a malformed message produces warnings in a yellow banner, never a
crash: broken MIME, invalid base64, unknown charset, missing headers, garbage
input and empty input all have tests.

---

## 5. Testing

```bash
python tests/test_parser.py            # 38 unit tests, ~0.5 s
python tests/make_samples.py           # 6 realistic .eml files
python main.py --eml-dir samples       # manual check of the whole UI
```

The unit tests cover each layer against synthetic messages: header decoding
(base64/QP/unknown charset/folded/CJK aliases), charset fallbacks (UTF-16 BOM
over a lying `us-ascii` declaration, Shift-JIS, GBK), structure (alternative
selection, nested mixed/related/alternative with a CID image and a PDF, lazy
attachment sizing, forwarded messages, attachments without a file name),
robustness (missing headers, wrong boundary, corrupt base64, random bytes, empty
input) and the sanitiser (script removal, `javascript:` URLs, tracking pixels,
remote-image blocking, CID resolution, table/link preservation, tag balancing,
CSS filtering).

The generated samples exercise the paths that are hard to unit-test - real
rendering, inline images, CJK fonts, blocked-image banners - in both renderers:

```bash
MAILVIEWER_NO_WEBENGINE=1 python main.py --eml-dir samples   # QTextBrowser path
```

Manual checklist for a new mailbox: fetch unread, select a message with
attachments, save one and save all, toggle "Show remote images", search the
list, use "Find in message", open a message with no HTML part, and confirm that
messages stay unread unless "Mark messages as read" is enabled.

---

## 6. Notes on this machine

`qt_bootstrap.py` exists because PySide6 fails to import under Miniconda here:
the interpreter directory and `%PATH%` (which conda fills with `Library\bin`)
are searched for DLLs before the `PySide6` package directory, so Qt binds
against conda's older copies of its dependencies and raises

```
ImportError: DLL load failed while importing QtCore: The specified procedure could not be found.
```

The bootstrap loads Qt's core libraries with `LOAD_WITH_ALTERED_SEARCH_PATH`
before PySide6 is imported, which resolves the dependencies from the PySide6
directory. It is a no-op when the plain import already works.
