You are a Senior Python Developer, Senior Email Protocol Engineer, and Senior Desktop UI Engineer.

## Goal

Build a Python application that receives emails using my existing Python script and displays them in a human-readable format.

My existing script already downloads emails successfully.
DO NOT change the email receiving logic unless absolutely necessary.
Instead, build a parser and viewer on top of it.

I will provide my existing Python code.

---

## Requirements

The downloaded email is currently a raw MIME message containing:

- HTML
- Plain text
- MIME multipart sections
- Attachments
- Embedded images
- Various encodings
- Quoted printable
- Base64
- Different charsets

The application must analyze the MIME structure automatically and present the email like a normal email client.

---

## Features

### 1. Parse MIME Structure

Correctly handle:

- multipart/mixed
- multipart/alternative
- multipart/related
- text/plain
- text/html
- nested multipart sections

Automatically locate the actual message body.

---

### 2. Decode Content

Support decoding:

- Base64
- Quoted Printable
- UTF-8
- UTF-16
- ISO-8859-1
- GBK
- EUC-KR
- Shift-JIS
- Unknown charset (fallback safely)

Never crash because of an unknown charset.

---

### 3. HTML Processing

Convert HTML into readable content.

Requirements:

- Remove unnecessary HTML
- Remove scripts
- Remove styles
- Remove tracking pixels
- Decode HTML entities
- Preserve formatting
- Preserve paragraphs
- Preserve tables when possible
- Preserve hyperlinks
- Preserve inline images when possible

The output should look similar to Gmail or Outlook.

---

### 4. Email Header

Display

From
To
CC
BCC
Subject
Date
Reply-To
Message-ID

Decode encoded words such as

=?UTF-8?B?...?=

correctly.

---

### 5. Attachments

Detect attachments.

Display

Filename
Size
Content Type

Allow saving attachments.

---

### 6. Embedded Images

Detect CID images.

Display them inside the HTML if possible.

If not possible, show placeholders.

---

### 7. Plain Text

If HTML does not exist

display the plain text version.

---

### 8. UI

Create a clean desktop interface.

Preferred:

PySide6

Alternative:

PyQt6

Features:

Left panel

- email list

Right panel

- From
- Subject
- Date
- HTML viewer

Bottom

- attachment list

Support:

scrolling
search
copy
select text

---

### 9. Error Handling

Never terminate because of malformed email.

Gracefully handle

broken MIME
invalid encoding
missing headers
unknown charset
broken HTML

---

### 10. Code Organization

Organize the project into modules.

Example:

mail_receiver.py
mail_parser.py
mime_decoder.py
html_processor.py
attachment_manager.py
models.py
viewer.py
main.py

Avoid putting everything into one file.

---

### 11. Code Quality

Use

type hints

dataclasses

classes where appropriate

logging

exception handling

clean architecture

Readable code is more important than clever code.

---

### 12. Libraries

Prefer standard library first.

Recommended libraries when necessary:

email
email.policy
email.header
html
BeautifulSoup4
lxml
bleach
chardet
charset-normalizer

Only introduce dependencies when they provide clear benefits.

---

### 13. Performance

Some emails exceed 20 MB.

Avoid unnecessary copying.

Parse incrementally where possible.

---

### 14. Deliverables

For every implementation step provide:

1. Design explanation
2. File structure
3. Complete source code
4. Reasoning
5. Testing strategy

---

### Development Policy

Do NOT rewrite my existing email retrieval code.

Integrate with it.

If the existing code has problems, explain them before changing anything.

If there are multiple implementation choices, explain the tradeoffs and recommend the most robust solution.

When parsing MIME messages, follow RFC-compliant behavior whenever practical.

Focus on correctness, maintainability, and compatibility with real-world emails from providers such as Gmail, Outlook, Yahoo, Apple Mail, and Exchange.