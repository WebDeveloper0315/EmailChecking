"""Entry point.

    python main.py                      # open the viewer
    python main.py --eml mail.eml       # open the viewer on local .eml files
    python main.py --dump unread        # print messages as text (no GUI)
    python main.py --debug              # verbose logging
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from config import AppSettings

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def configure_logging(level: str, log_file: str | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        try:
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        except OSError:
            print(f"Cannot write to {log_file}; logging to the console only", file=sys.stderr)
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format=LOG_FORMAT, handlers=handlers)
    logging.getLogger("imbox").setLevel(logging.WARNING)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read and display e-mail in a readable form.")
    parser.add_argument("--eml", nargs="*", metavar="FILE",
                        help="open local .eml files instead of connecting to IMAP")
    parser.add_argument("--eml-dir", metavar="DIR",
                        help="open every .eml file in a folder")
    parser.add_argument("--dump", nargs="?", const="unread", metavar="FILTER",
                        help="print messages to the console instead of opening the GUI "
                             "(FILTER: unread, today, all or a sender address)")
    parser.add_argument("--config", metavar="FILE", help="path to config.ini")
    parser.add_argument("--limit", type=int, help="maximum number of messages to fetch")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    parser.add_argument("--log-file", metavar="FILE", help="also write the log to a file")
    return parser.parse_args(argv)


def collect_paths(args: argparse.Namespace) -> list[str]:
    paths: list[str] = list(args.eml or [])
    if args.eml_dir:
        paths.extend(str(p) for p in sorted(Path(args.eml_dir).glob("*.eml")))
    return paths


def dump_to_console(settings: AppSettings, criteria: str, paths: list[str]) -> int:
    """Headless mode - the notebook's output, but properly decoded."""
    from html_processor import html_to_text
    from mail_parser import parse_email
    from mail_receiver import EmlFileSource, ImboxReceiver, ReceiveError
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
            with ImboxReceiver(settings.account) as receiver:
                for raw in receiver.fetch(criteria, settings.fetch_limit):
                    show(parse_email(raw.raw, uid=raw.uid, source=raw.folder))
                    count += 1
                    if settings.mark_seen:
                        receiver.mark_seen(raw.uid)
        except ReceiveError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    print(f"{count} message(s).")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    configure_logging("DEBUG" if args.debug else "INFO", args.log_file)

    settings = AppSettings.load(args.config)
    if args.limit:
        settings.fetch_limit = args.limit

    paths = collect_paths(args)

    if args.dump is not None:
        return dump_to_console(settings, args.dump, paths)

    try:
        import viewer
    except ImportError as exc:
        print(
            f"The graphical viewer needs PySide6 ({exc}).\n"
            "Install it with:  pip install PySide6\n"
            "Or run without a GUI:  python main.py --dump unread",
            file=sys.stderr,
        )
        return 3

    return viewer.run(settings, paths)


if __name__ == "__main__":
    raise SystemExit(main())
