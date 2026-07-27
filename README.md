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
python tests/run_all.py             # 215 tests
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
mail_storage.py       message store: index + raw cache + parsed-message LRU
mail_database.py      SQLite index: one row per message, sync bookmarks per folder
theme.py              light/dark palettes and the application stylesheet
outbox.py             disk queue for unsent mail, with exponential backoff
notifications.py      system tray icon, unread badge, new-mail toasts
mail_parser.py        raw bytes -> models.Email (MIME tree walk)
mime_decoder.py       charsets, transfer encodings, RFC 2047 headers, dates
html_processor.py     HTML whitelist sanitiser, text<->HTML
attachment_manager.py saving, safe file names, data: URIs for inline images
models.py             Address, Attachment, Email dataclasses
config.py             config.ini + environment settings
logging_setup.py      structured JSON logging with password redaction
qt_bootstrap.py       fixes PySide6 DLL loading on Windows/conda
tests/                fake IMAP + fake SMTP servers, 215 tests
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
whole suite (215 tests, ~55 s).

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
| 19 | **Readable in every theme** | Fusion style + complete palette + stylesheet applied together; muted text uses a real colour instead of an alpha channel | Reproduced first: Windows reported `AppsUseLightTheme = 0` while the app theme was `light`. Screenshots of the light and dark themes taken from the running client; toolbar, folder tree, headers and status bar all legible in both | `theme.py`, `viewer.py` |
| 20 | **Incremental sync** | SQLite index with per-folder `UIDVALIDITY` / highest UID / `HIGHESTMODSEQ`; new mail by UID range, flag changes by `CHANGEDSINCE`, deletions only when counts disagree | `test_sync`: the second pass asserts the FETCH ranges - a `6:*` style range is present and no unqualified `1:*` scan remains; a server without CONDSTORE still full-scans; a restart downloads nothing and still lists everything. `test_database` (19): bookmarks survive reopening, prune, per-account isolation, closed-index guard | `mail_database.py`, `mail_storage.py`, `mail_sync.py`, `mail_receiver.py`, `models.py` |
| 21 | **Delete reaches the server** | Verified delete/move returning `(ok, still_there)`; the trash folder is passed explicitly instead of being guessed by the worker; rows are restored when the server kept the message; a sync follows every successful delete | `test_receiver`: a server configured to answer OK without deleting is detected for both delete and move, and `present_uids` reports what is left. `test_gui_integration`: delete-to-trash and permanent delete still assert the server state | `mail_receiver.py`, `mail_sync.py`, `viewer.py` |
| — | **Flag bug from the index** | Rows restored from the index are independent objects, so worker flag updates never reached the displayed row - "unstar" re-starred. Flags are now written into the row, and clicks apply optimistically | Caught by the GUI suite; `test_star_and_unstar` and `test_mark_read_and_unread` now pass twice in a row | `viewer.py` |
| 16 | **Multiple accounts** | Profiles in `config.ini`; one store + one sync thread + one IMAP connection per account; account roots in the folder tree; From picker in compose | `test_outbox_config` (20): round trip, migration of an old file, distinct cache keys, per-profile SMTP, env override hits only the active profile. `test_gui_integration`: two accounts sync in parallel against two fake servers, switching swaps the list, starring in "Work" leaves "Personal" untouched | `config.py`, `viewer.py`, `mail_sync.py`, `compose_window.py`, `settings_dialog.py` |
| 17 | **New-mail notifications** | Tray icon with unread badge, toast per arrival, click opens the message, first sync stays silent | `test_gui_integration`: first sync raises no toast, an arriving message raises exactly one, an already-read arrival raises none, badge tracks the mailbox, icon renders with and without a badge | `notifications.py`, `viewer.py`, `config.py` |
| 18 | **Outbox + SMTP fallback** | Unsent mail is queued to disk and retried with backoff; ports 587/465/25 are tried in turn; failures are classified retryable or not | `test_outbox_config`: persistence across a restart, backoff growth and cap, per-account filtering, exhaustion. `test_sender`: TLS failure on 587 falls back to 465, auth failure is *not* retryable. `test_gui_integration`: a failed send queues, a later retry delivers it and files it in Sent | `outbox.py`, `mail_sender.py`, `viewer.py`, `compose_window.py` |
| — | **Threading defect** | Worker signals were connected to lambdas, which Qt delivers *directly*, so UI handlers ran inside the sync thread. Signals now carry the account name and the window connects bound methods | The new GUI tests exposed it as "QObject: Cannot create children for a parent in a different thread"; after the fix the whole suite runs clean | `mail_sync.py`, `viewer.py` |
| 14 | **Architecture** | Twelve focused modules, largest is the UI | — | all |
| 15 | **UI** | Folder tree, list, preview, attachment pane, status bar, progress bar, unread count, sync indicator | Screenshots taken from the running client against the fake server | `viewer.py` |

### What could **not** be verified here, and needs your testing

1. **Sending a real message — SMTP is blocked by the VPN on this machine.**
   Diagnosed step by step:

   | Check | Result |
   |---|---|
   | Windows Firewall outbound policy | `DefaultOutboundAction = Allow` on all profiles; no rule matches 25/465/587 |
   | `smtp.gmail.com` 587 / 465 / 25 | all time out |
   | `smtp.gmail.com:443` (same host, web port) | also times out |
   | `imap.gmail.com:993`, `example.com:80` | work |
   | DNS | resolves to real Google IPs; no sinkhole, no hosts entry |
   | Default route | `AmneziaVPN` (metric 0), Tailscale also present |
   | Same connection bound to the Ethernet address | `PermissionError` (WSAEACCES) even for IMAP |

   So the block is the **AmneziaVPN tunnel**: its exit does not carry SMTP, and
   its kill switch prevents bypassing it. That is a VPN policy — it cannot be
   changed from inside this application or from Windows Firewall. To send:
   disconnect the VPN, exclude the application (split tunnelling), or use an
   exit that permits SMTP. Port 465 is worth trying first; the client now falls
   back to it automatically.

   Everything up to the socket is covered by the fake-SMTP tests (build, port
   fallback, TLS choice, login, envelope, Sent copy, error classification), and
   a blocked send is no longer lost — it lands in the Outbox and goes out when
   the network allows it.
2. **Provider quirks on real folders**: Gmail's `[Gmail]/All Mail` duplicates
   every message (it is a label, not a folder), and Gmail applies its own
   "delete = move to Trash" semantics. Folder listing is verified against your
   account; deleting and moving are verified only against the fake server.
3. **Large mailboxes.** Your Inbox has 104 messages and All Mail 4701; the
   default cap is 200 newest per folder (Settings → Synchronisation). Sync
   timing on All Mail is untested.
4. **`mark_seen = True` is set in your `config.ini`**, so opening a message
   marks it read on the server. Turn it off in Settings → Accounts if that is
   not wanted.
5. **Notifications need a system tray.** Windows provides one, and the toast was
   exercised through the notification centre in the tests, but whether Windows
   Focus Assist suppresses the banner on your desktop is yours to confirm.

### Checking the logs yourself

```bash
python main.py --debug
type logs\mailviewer.log | findstr "\"component\":\"imap\""
findstr /i "password" logs\mailviewer.log     # must find nothing
```

### Multiple accounts, notifications and the outbox

**Several accounts at once.** `config.ini` holds a list of profiles
(`[account:Personal]`, `[smtp:Personal]`, …); each one gets its own
`MailStore`, its own `SyncController` and therefore its own IMAP connection and
thread, so all accounts synchronise in parallel. The folder tree shows one root
per account, and selecting a folder under another root switches to it. Compose
has a **From** picker: the chosen account decides which SMTP server is used and
which Sent folder receives the copy. An old single-account `config.ini` is
migrated automatically.

**New-mail notifications.** A tray icon carries an unread badge (drawn at
runtime, no image files) and new mail raises a native toast naming the sender
and subject; clicking it opens that message. The *first* sync of a folder is
deliberately silent - loading 200 existing messages is not new mail. Only
unread arrivals notify, `only_inbox` is on by default, and closing the window
keeps the client in the tray (the tray menu has a real Quit) so mail can still
be announced.

**Outbox.** When the server cannot be reached, the message is not lost: it is
written to `cache/outbox/` as `.eml` + `.json` and retried with exponential
backoff (1, 2, 4 … minutes, capped at 30, given up after 20 attempts). Only
*retryable* failures are queued - a wrong password or a rejected recipient is
reported instead, because retrying would fail identically forever. The status
bar shows how many messages are waiting.

**SMTP port fallback.** If the configured port times out, the other standard
pairs are tried automatically (587/STARTTLS → 465/SSL → 25). If none answer, the
error message includes a probe of all three ports and names the usual causes
(VPN, ISP, corporate firewall) instead of just reporting a timeout.

### Theme, local index and verified deletes

**One theme, applied as a unit.** Setting only a palette is not enough on
Windows: the native *windows11* style paints tool buttons and menu text with the
colours of the **operating system's** light/dark setting. With Windows in dark
mode and the application forced to light, toolbar labels came out white on
white. `theme.py` now applies the Fusion style, a complete palette (including
the Disabled group) and a matching stylesheet together, so a theme is exactly
what it says regardless of the OS setting. `system` follows the desktop through
`QStyleHints.colorScheme()`, with the registry as a fallback.

**A modern look.** Flat toolbars with hover states, rounded inputs with an
accent focus ring, soft rounded row selection, slim scrollbars, styled tabs,
menus and group boxes - all generated from the same palette, so light and dark
stay consistent.

**A local index instead of a full rescan.** `mail_database.py` keeps one SQLite
row per message (sender, subject, date, size, flags, preview) plus, per folder,
`UIDVALIDITY`, the highest UID seen and `HIGHESTMODSEQ`. A sync then asks only
for the difference:

| | before | now |
|---|---|---|
| new mail | `UID FETCH 1:*` over the whole mailbox | `UID FETCH <highest+1>:*` |
| flag changes | compared every message | `CHANGEDSINCE <modseq>` (RFC 7162) |
| deletions | implied by the full scan | UID list requested *only* when the count disagrees |
| start-up | re-parsed every cached `.eml` | one SQL query; bodies parsed when opened |

A mailbox with 1800 messages therefore costs one small FETCH per interval
instead of a full listing, and the window fills immediately. Servers without
CONDSTORE (rare) keep the old single-round-trip full scan.

**Deletes are verified.** `ImapClient.delete()` and `.move()` now re-check the
source folder afterwards and return `(ok, still_there)`. Several servers answer
OK to a STORE/EXPUNGE that does not actually remove anything, and the previous
code trusted that answer - the row vanished from the list while the mail was
still in the mailbox. Now the row is only dropped for messages the server
confirms are gone, anything it kept is put back in the list with an explanatory
message, and a successful delete is followed by a (cheap) sync so the list and
the mailbox cannot drift apart.

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
python tests/run_all.py              # 215 tests, ~55 s
python tests/run_all.py --no-gui     # 175 tests, ~5 s, no windows
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
| `test_sender.py` | 33 | message building, reply/reply-all/forward, SMTP conversation and failures |
| `test_sync.py` | 18 | incremental sync, dedup, cache, UIDVALIDITY, cancellation, failures |
| `test_logging.py` | 7 | structured JSON fields, password redaction in messages, args and extras |
| `test_outbox_config.py` | 20 | outbox persistence/backoff/exhaustion, multi-account config round trip and migration |
| `test_database.py` | 22 | SQLite index, sync bookmarks, summaries, lazy body loading, pruning |
| `test_gui_integration.py` | 40 | the real window against the fake server: startup, sync, flags, delete, search, sort, compose, folders, auth failure |

---

## 6. Known limitations

* **Send later** is not implemented as a scheduler. The Outbox covers the
  related need (a message that cannot go now is retried automatically), but
  "send this at 09:00 tomorrow" would need a process that outlives the window.
* **One notification style.** Toasts go through the system tray; there is no
  in-app notification list or per-sender rule.
* **Accounts are independent.** There is no unified inbox across accounts; each
  account keeps its own folder tree and message list.
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
