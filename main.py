"""Entry point.

    python main.py                      # open the mail client
    python main.py --eml mail.eml       # open local .eml files
    python main.py --dump unread        # print messages as text (no GUI)
    python main.py --check              # test the IMAP/SMTP configuration
    python main.py --debug              # verbose logging
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import logging_setup
from config import AppSettings


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read, send and manage e-mail.")
    parser.add_argument("--eml", nargs="*", metavar="FILE",
                        help="open local .eml files instead of connecting to IMAP")
    parser.add_argument("--eml-dir", metavar="DIR",
                        help="open every .eml file in a folder")
    parser.add_argument("--dump", nargs="?", const="unread", metavar="FILTER",
                        help="print messages to the console instead of opening the GUI "
                             "(FILTER: unread, today, all, flagged or a sender address)")
    parser.add_argument("--folder", metavar="NAME", help="mailbox to read (default INBOX)")
    parser.add_argument("--check", action="store_true",
                        help="verify the IMAP login and list the folders, then exit")
    parser.add_argument("--config", metavar="FILE", help="path to config.ini")
    parser.add_argument("--limit", type=int, help="maximum number of messages to fetch")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    parser.add_argument("--log-dir", metavar="DIR", help="where to write mailviewer.log")
    return parser.parse_args(argv)


def collect_paths(args: argparse.Namespace) -> list[str]:
    paths: list[str] = list(args.eml or [])
    if args.eml_dir:
        paths.extend(str(p) for p in sorted(Path(args.eml_dir).glob("*.eml")))
    return paths


def check_configuration(settings: AppSettings) -> int:
    """``--check``: prove that the credentials and servers work."""
    from mail_receiver import ImapClient, ReceiveError

    account = settings.account
    print(f"IMAP  {account.username}@{account.host}:{account.port} "
          f"(ssl={account.use_ssl}, starttls={account.starttls})")
    try:
        with ImapClient(account) as client:
            folders = client.list_folders(with_counts=True)
            print(f"  connected, {len(folders)} folder(s):")
            for folder in folders:
                marker = f" [{folder.kind}]" if folder.kind != "other" else ""
                print(f"    {folder.display:<24} {folder.total:>5} messages, "
                      f"{folder.unread:>4} unread{marker}")
    except ReceiveError as exc:
        print(f"  FAILED: {exc}", file=sys.stderr)
        return 2

    smtp = settings.smtp_settings()
    print(f"SMTP  {smtp.username}@{smtp.host}:{smtp.port} ({smtp.security})")
    if not smtp.is_complete:
        print("  not configured (Settings -> Sending)")
        return 0
    import smtplib
    import ssl

    try:
        if smtp.security == "ssl":
            server = smtplib.SMTP_SSL(smtp.host, smtp.port, timeout=smtp.timeout,
                                      context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(smtp.host, smtp.port, timeout=smtp.timeout)
        server.ehlo()
        if smtp.security == "starttls":
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
        server.login(smtp.username, smtp.password)
        server.quit()
        print("  login accepted")
    except Exception as exc:
        print(f"  FAILED: {exc}", file=sys.stderr)
        return 3
    return 0


def dump_to_console(settings: AppSettings, criteria: str, paths: list[str],
                    folder: str = "INBOX") -> int:
    """Headless mode - the notebook's output, but properly decoded."""
    from html_processor import html_to_text
    from mail_parser import parse_email
    from mail_receiver import EmlFileSource, ImapClient, ReceiveError
    from models import format_addresses

    def show(mail) -> None:
        print("=" * 70)
        print(f"From    : {format_addresses(mail.from_addrs)}")
        print(f"To      : {format_addresses(mail.to_addrs)}")
        if mail.cc_addrs:
            print(f"Cc      : {format_addresses(mail.cc_addrs)}")
        print(f"Date    : {mail.display_date}")
        print(f"Subject : {mail.display_subject}")
        if mail.warnings:
            print(f"Warnings: {'; '.join(mail.warnings)}")
        body = mail.text_body or html_to_text(mail.html_body)
        print("-" * 70)
        print(body.strip() or "(no readable body)")
        if mail.attachments:
            print("-" * 70)
            for attachment in mail.attachments:
                print(f"[attachment] {attachment.filename} "
                      f"({attachment.size} bytes, {attachment.content_type})")
        print()

    count = 0
    if paths:
        for raw in EmlFileSource.read(paths):
            show(parse_email(raw.raw, uid=raw.uid))
            count += 1
    else:
        try:
            with ImapClient(settings.account) as client:
                for raw in client.fetch(criteria, settings.fetch_limit, folder=folder):
                    show(parse_email(raw.raw, uid=raw.uid, source=raw.folder,
                                     folder=raw.folder, flags=raw.flags))
                    count += 1
                    if settings.mark_seen:
                        client.mark_seen(raw.uid, folder)
        except ReceiveError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    print(f"{count} message(s).")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    log_path = logging_setup.configure("DEBUG" if args.debug else "INFO", args.log_dir)

    settings = AppSettings.load(args.config)
    logging_setup.register_secret(settings.account.password)
    logging_setup.register_secret(settings.smtp.password)
    if args.limit:
        settings.fetch_limit = args.limit
        settings.sync.max_messages_per_folder = args.limit

    folder = args.folder or settings.account.folder or "INBOX"
    paths = collect_paths(args)

    if args.check:
        return check_configuration(settings)
    if args.dump is not None:
        return dump_to_console(settings, args.dump, paths, folder)

    try:
        import viewer
    except ImportError as exc:
        print(
            f"The graphical client needs PySide6 ({exc}).\n"
            "Install it with:  pip install PySide6\n"
            "Or run without a GUI:  python main.py --dump unread",
            file=sys.stderr,
        )
        return 3

    if log_path is not None:
        print(f"Logging to {log_path}", file=sys.stderr)
    return viewer.run(settings, paths)


if __name__ == "__main__":
    raise SystemExit(main())
