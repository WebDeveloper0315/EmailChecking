# Mail Viewer — desktop e-mail client

A PySide6 mail client built **on top of** the existing `email-check.ipynb` retrieval
code: imbox still connects and still runs the same searches, and everything else —
MIME parsing, HTML sanitising, synchronisation, sending, folder and flag management —
is layered around it.

```bash
pip install -r requirements.txt

python main.py                      # the client
python main.py --check              # verify IMAP + SMTP configuration, then exit
python main.py --eml-dir samples    # open local .eml files, no account needed
python main.py --dump unread        # print messages as text (no GUI)
python tests/run_all.py             # 148 tests
```

---

## 1. The bug: fetching failed

### Root cause

**imbox changed its constructor in 0.10 and the installed version is 0.10.1.**

```python
# imbox 0.9.8 — what the notebook and the receiver called
Imbox(hostname, username=..., password=..., ssl=..., ssl_context=None, starttls=...)

# imbox 0.10.1 — what is installed (imbox/imbox.py:15)
Imbox(config: Config, policy=None, vendor=None)
```

The old call therefore died with

```
TypeError: Imbox.__init__() got an unexpected keyword argument 'username'
```

*before a socket was ever opened* — which is why it looked like a connection problem
but never produced a network error. Reproduced deterministically offline by making
`ImapTransport.__init__` raise: the failure happens earlier, so it can only be an API
mismatch.

Two further 0.10 changes mattered:

| Change | Consequence |
|---|---|
| Messages yield `EmailObject(parsed=…, uid, flags)` | `message.raw_email` no longer exists, so the old code silently fell back to a **second** network fetch per message |
| `parse_email` stores `raw_email = str_encode(raw, charset, errors="ignore")` (imbox/parser.py:246) | The raw bytes are **lossy**: any part not in the top-level charset loses bytes. Unusable as parser input |

### The fix, and why it is correct

`mail_receiver._open_imbox()` inspects `Imbox.__init__`'s signature and calls whichever
API is installed — the `Config` form on ≥ 0.10, the keyword form on 0.9.x. Your
retrieval logic is untouched: `search_uids()` still goes through
`imbox.messages(**query)` with the same four queries (`all` / `unread` / `today` /
sender), so the search semantics stay imbox's.

Message bodies are then fetched once with `UID FETCH … (BODY.PEEK[])` on imbox's own
connection (`imbox.connection`). That is correct because it (a) returns the untouched
bytes instead of imbox's lossy string, (b) does not set `\Seen`, and (c) avoids the
double download the fallback path was doing.

**Verified against your real account** (read-only, `python main.py --check`):

```
IMAP  blackghost1503@gmail.com@imap.gmail.com:993 (ssl=True, starttls=False)
  connected, 9 folder(s):
    Inbox                      104 messages,    1 unread [inbox]
    Drafts                       0 messages,    0 unread [drafts]
    Sent Mail                    6 messages,    0 unread [sent]
    Spam                         1 messages,    1 unread [spam]
    Trash                        0 messages,    0 unread [trash]
    All Mail                  4701 messages,    2 unread [all]
```

### Other bugs found and fixed

| Bug | Symptom | Fix |
|---|---|---|
| Infinite loop in the old `fetch()` | A persistent per-message error made `while True: … except: continue` spin forever, freezing the worker | Replaced by explicit UID iteration with per-message error handling |
| `message/rfc822` attachments were base64-encoded | "Forward as .eml" produced an attachment no client could open (RFC 2046 §5.2.1 forbids base64 there) | `build_message()` attaches a parsed message object, so the stdlib emits an 8bit part |
| `message/rfc822` parts that *are* base64 could not be read | Other clients do this; our parser returned the encoded text as the `.eml` | The parser now decodes the CTE of a `message/rfc822` part |
| Broken multipart boundary | The whole body became a nameless `attachment.bin` and the text was lost | Stranded text is shown with a warning |
| UTF-16 BOM left in the text | `﻿` at the start of decoded bodies | BOM-aware codecs (`utf-16`, not `utf-16-le`) |
| Attachment path traversal | `../../evil.txt` escaped the download folder | `sanitize_filename()` strips directories first |
| Qt profile lifetime | "Release of profile requested but WebEnginePage still not deleted" on exit | The page is released before the shared profile |

---

## 2. Architecture

```
main.py               entry point, CLI (--check / --dump / --eml), logging set-up
viewer.py             main window: folder tree, list, preview, actions
compose_window.py     rich-text compose: attachments, drag & drop, inline images
settings_dialog.py    account / sending / sync / appearance, with a connection test
mail_sync.py          FolderSynchronizer (pure Python) + SyncWorker/SyncController (Qt)
mail_receiver.py      IMAP: imbox compatibility, folders, search, flags, move, append
mail_sender.py        SMTP: message building, reply/forward composition, sending
mail_storage.py       message store: dedup, flags, unread counts, on-disk .eml cache
mail_parser.py        raw bytes -> models.Email (MIME tree walk)
mime_decoder.py       charsets, transfer encodings, RFC 2047 headers, dates
html_processor.py     HTML whitelist sanitiser, text<->HTML
attachment_manager.py saving, safe file names, data: URIs for inline images
models.py             Address, Attachment, Email dataclasses
config.py             config.ini + environment settings
logging_setup.py      structured JSON logging with password redaction
qt_bootstrap.py       fixes PySide6 DLL loading on Windows/conda
tests/                fake IMAP + fake SMTP servers, 148 tests
```

**Threading.** The UI thread performs no network calls. `SyncController` owns one
`QThread`; requests are Qt signals connected to the worker's slots, so Qt queues them
automatically. One IMAP connection lives in that thread (imaplib is not thread safe),
which also serialises commands. Sending uses its own short-lived thread per message.

**Sync algorithm** (one folder, one pass): read `UIDVALIDITY`, then a single
`UID FETCH 1:* (UID FLAGS RFC822.SIZE)`. UIDs we know but the server no longer lists
are removals; UIDs on both sides with different flags are flag updates (no download);
unknown UIDs get their body downloaded, newest first, capped — or read from the disk
cache if a previous run already stored them. A quiet mailbox costs exactly one FETCH
per interval, and nothing is rebuilt, so the selection and scroll position survive.

---

## 3. Verification report

Everything below was executed on this machine. `python tests/run_all.py` runs the
whole suite (148 tests, ~55 s).

| # | Feature | What changed / why | How it was verified | Files |
|---|---|---|---|---|
| — | **Fetch bug** | imbox 0.10 API compatibility; single `BODY.PEEK[]` fetch instead of imbox's lossy `raw_email` | `test_receiver` (29): connects, searches, byte-exact fetch of a latin-1 body, legacy-signature branch. **Real Gmail:** `--check` connected and listed 9 folders | `mail_receiver.py` |
| 1 | **Auto sync** | `FolderSynchronizer` + `SyncController` with a `QTimer`; intervals 30 s/1/5/10/30 min/manual | `test_sync` (18): new mail, flag change, removal, no re-download on a second pass, cache reuse after "restart", UIDVALIDITY reset, cancellation. `test_gui_integration`: new mail appears once, **selection survives a sync**, UI keeps ticking during a 1 s download | `mail_sync.py`, `mail_storage.py`, `config.py` |
| 2 | **Send** | `build_message()` (plain/HTML/attachments/inline CID/UTF-8/Reply-To/priority) + `SmtpSender` | `test_sender` (29): every message built is parsed back with our own parser; Bcc absent from the sent copy but in the envelope; STARTTLS/SSL paths; auth, refusal, disconnect and TLS errors. GUI test sends through a fake SMTP and asserts the copy lands in Sent | `mail_sender.py`, `compose_window.py` |
| 3 | **Reply / Reply all** | Threading headers (`In-Reply-To`, `References`), quoting in text **and** HTML, self excluded from Reply all | `test_sender`: recipients, threading survives a round trip, quoting in both formats, original scripts/trackers stripped from the quote, no double `Re:`. GUI test checks the compose window is pre-filled | `mail_sender.py` |
| 4 | **Forward** | Inline (headers block + body + attachments carried) and as `.eml` | `test_sender`: inline keeps attachments; the `.eml` attachment is re-parsed and its subject read back | `mail_sender.py`, `mail_parser.py` |
| 5 | **Delete** | Move to Trash (auto-detected) or permanent, with a confirmation dialog; the row disappears immediately | GUI test answers the dialog and asserts the server state for both paths | `viewer.py`, `mail_receiver.py`, `mail_sync.py` |
| 6 | **Folders** | `LIST` parsing with RFC 6154 special-use flags, name heuristics fallback, IMAP UTF-7 decoding, nested tree with unread counts | `test_receiver`: special-use, name fallback (incl. `Papierkorb`), `\Noselect`, UTF-7 round trip, counts. GUI test switches folders. Real Gmail: all six special folders classified correctly | `mail_receiver.py`, `viewer.py` |
| 7 | **Read status / star** | `\Seen` and `\Flagged` via `UID STORE`, optimistic UI update | `test_receiver` (add/remove), GUI test asserts server flags after clicking | `mail_receiver.py`, `viewer.py` |
| 8 | **Search** | Incremental client-side search over subject/sender/recipient/date/body/attachment name, plus a server-side `TEXT` search | GUI tests: incremental narrowing, per-field scoping, body search. `test_receiver`: server-side search | `viewer.py`, `models.py`, `mail_receiver.py` |
| 9 | **Sorting** | Date, sender, subject, size, read status, both directions | GUI tests: sorted order asserted, selection preserved across a re-sort | `viewer.py` |
| 10 | **Threading** | No network call on the UI thread | GUI test starts a 10 ms timer, makes the fake server sleep 50 ms per message, and asserts the timer kept firing (>20 ticks) during the download | `mail_sync.py` |
| 11 | **Errors** | Timeout, auth, DNS, refused, TLS, disconnect → one message each, with provider hints | `test_receiver` + `test_sender` cover each branch; GUI test shows a wrong password ends as "Sync failed" with an empty list, not a crash | `mail_receiver.py`, `mail_sender.py` |
| 12 | **Logging** | Console + rotating JSON `logs/mailviewer.log`; a filter scrubs registered secrets and `password=`/`LOGIN` shapes | `test_logging` (7): structured fields survive, registered secrets, `password=` text, `LOGIN` commands and secret-named keys are all masked. Also checked on the real log file written during the Gmail `--check`: 0 occurrences of the stored app password | `logging_setup.py` |
| 13 | **Configuration** | `config.ini` with `[account] [smtp] [sync] [viewer] [window]`, environment overrides, window geometry remembered | Round-tripped by the GUI tests (each builds settings, saves on close) | `config.py`, `settings_dialog.py` |
| 14 | **Architecture** | Twelve focused modules, largest is the UI | — | all |
| 15 | **UI** | Folder tree, list, preview, attachment pane, status bar, progress bar, unread count, sync indicator | Screenshots taken from the running client against the fake server | `viewer.py` |

### What could **not** be verified here, and needs your testing

1. **Sending a real message.** Outbound SMTP is blocked on this machine — ports 587,
   465 and 25 all time out (`WinError 10060`), so `--check` cannot complete the SMTP
   half. Everything up to the socket is covered by the fake-SMTP tests (build, TLS
   choice, login, envelope, Sent copy, error handling), but the first real send is
   yours to run. If 587 is blocked on your network too, switch to
   Settings → Sending → *SSL/TLS (port 465)*.
2. **Provider quirks on real folders**: Gmail's `[Gmail]/All Mail` duplicates every
   message (it is a label, not a folder), and Gmail applies "delete = move to Trash"
   semantics of its own. Folder listing is verified against your account; deleting and
   moving are verified only against the fake server.
3. **Large mailboxes.** Your Inbox has 104 messages and All Mail 4701; the default cap
   is 200 newest per folder (Settings → Synchronisation). Sync timing on All Mail is
   untested.
4. **`mark_seen = True` is set in your `config.ini`**, so opening a message marks it
   read on the server. Turn it off in Settings → Account if that is not wanted.

### Checking the logs yourself

```bash
python main.py --debug
type logs\mailviewer.log | findstr "\"component\":\"imap\""
findstr /i "password" logs\mailviewer.log     # must find nothing
```

---

## 4. Features

**Synchronisation** — automatic refresh at 30 s / 1 / 5 / 10 / 30 min or manual;
background thread; incremental (new, changed flags, removed); duplicate downloads
prevented in memory *and* across restarts through the `.eml` cache; selection and
scroll position preserved; sync indicator and progress bar in the status bar.

**Composing** — rich text (bold/italic/underline, lists, links, inline images),
plain-text mode, attachments via button or drag & drop, To/Cc/Bcc, UTF-8 subjects,
Reply-To, high priority, Save draft (APPEND to Drafts), sent copy stored in Sent.

**Reading** — folder tree with unread counts, three-line message rows with unread,
star and attachment markers, sanitised HTML rendering (QtWebEngine, QTextBrowser
fallback), inline CID images, remote images blocked by default, tracking pixels
removed, attachment saving and image preview, all-headers view.

**Managing** — reply, reply all, forward (inline or `.eml`), delete (Trash or
permanent, confirmed), move to any folder, mark read/unread, star/unstar, search
(6 fields, incremental, plus server-side), sort (5 keys, both directions).

---

## 5. Testing

```bash
python tests/run_all.py              # 148 tests, ~55 s
python tests/run_all.py --no-gui     # 122 tests, ~2 s, no windows
python tests/test_receiver.py -v     # one suite
python tests/make_samples.py         # regenerate samples/*.eml
```

The suites never touch a real server:

* `tests/fake_imap.py` — an in-memory IMAP server. imbox and `ImapClient` talk to it
  for real (LOGIN, LIST, STATUS, UID SEARCH/FETCH/STORE/COPY/MOVE/EXPUNGE, APPEND), so
  the tests exercise the actual retrieval code, not a mock of it.
* `tests/fake_smtp.py` — a fake `smtplib.SMTP` that records the conversation and can be
  scripted to fail (auth, refusal, disconnect, TLS).

| Suite | Tests | Covers |
|---|---|---|
| `test_parser.py` | 39 | MIME structure, charsets, RFC 2047, attachments, malformed input, HTML sanitising |
| `test_receiver.py` | 29 | imbox compatibility, search, byte-exact fetch, flags, move/delete, APPEND, folders, UTF-7, error translation |
| `test_sender.py` | 29 | message building, reply/reply-all/forward, SMTP conversation and failures |
| `test_sync.py` | 18 | incremental sync, dedup, cache, UIDVALIDITY, cancellation, failures |
| `test_logging.py` | 7 | structured JSON fields, password redaction in messages, args and extras |
| `test_gui_integration.py` | 26 | the real window against the fake server: startup, sync, flags, delete, search, sort, compose, folders, auth failure |

---

## 6. Known limitations

* **Send later** is not implemented. It needs a process that outlives the window;
  a desktop app that is closed cannot send. Drafts are stored on the server instead.
* **Threaded conversation view** is not implemented — messages are listed flat.
  Threading *headers* are written and preserved, so other clients thread the replies.
* **Server-side search** uses `TEXT "…"`, which most servers implement as a substring
  search over headers and body; per-field IMAP search keys are not exposed yet.
* **No OAuth2.** Gmail/Microsoft need an app password.
* **Inline images in a quoted reply** show as `[inline image: …]`; the original's
  images are not re-attached to the reply.
* **One account** at a time.
* `--dump` and `--check` remain available for headless use.

---

## 7. Notes on this machine

`qt_bootstrap.py` exists because PySide6 fails to import under Miniconda here: the
interpreter directory and `%PATH%` (which conda fills with `Library\bin`) are searched
for DLLs before the `PySide6` package directory, so Qt binds against conda's older
copies and raises `ImportError: DLL load failed while importing QtCore`. The bootstrap
pre-loads Qt's libraries with `LOAD_WITH_ALTERED_SEARCH_PATH`; it is a no-op when the
plain import already works.

`config.ini` currently stores your Gmail app password in clear text
(`remember_password = True`). Setting `MAIL_PASSWORD` in the environment and clearing
the file is safer; the app reads the environment first.
