"""Turn the HTML found in an e-mail into something safe and readable.

Mail HTML is hostile: it carries scripts, tracking pixels, remote beacons,
`position:fixed` overlays and, more often than not, tags that are never closed.
This module rewrites it with a *whitelist* parser built on ``html.parser``:

* unknown / dangerous tags are dropped, their text content is kept;
* ``script``, ``style``, ``svg``, ``iframe``, forms ... are dropped completely;
* only safe URL schemes survive, ``cid:`` images are resolved against the
  message's own inline parts;
* remote images are blocked by default (they are how senders track you) and can
  be enabled per message from the UI;
* tracking pixels are removed even when remote images are allowed;
* the tag stack is balanced on the way out, so broken input yields valid output.

``html.parser`` is used on purpose: it is lenient with malformed markup, it is
in the standard library, and it streams, which matters for 20 MB messages.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "SanitizedHtml",
    "sanitize_html",
    "text_to_html",
    "html_to_text",
    "build_document",
]

#: Tags that survive sanitising.  Anything not listed here is unwrapped: the
#: tag disappears but its text stays (that is what Gmail does with `<o:p>`).
ALLOWED_TAGS: frozenset[str] = frozenset(
    """
    a abbr address article aside b big blockquote br caption center cite code col
    colgroup dd del dfn div dl dt em fieldset figcaption figure font footer h1 h2
    h3 h4 h5 h6 header hr i img ins kbd legend li main mark nav ol p pre q s
    samp section small span strike strong sub sup table tbody td tfoot th thead
    time tr tt u ul var wbr
    """.split()
)

#: Dropped together with everything inside them: markup that either executes,
#: loads something, or holds no readable text.
DROP_WITH_CONTENT: frozenset[str] = frozenset(
    """
    script style head title meta link base iframe frame frameset noframes object
    embed applet param input select textarea svg math canvas audio video source
    track template xml
    """.split()
)

#: Not in ALLOWED_TAGS, so the tag disappears - but the text inside it stays.
#: ``form``, ``noscript`` and ``button`` regularly wrap *visible* wording in
#: marketing mail; dropping their contents made such messages look empty.
UNWRAPPED: frozenset[str] = frozenset(
    "form noscript button option optgroup fieldset legend".split()
)

VOID_TAGS: frozenset[str] = frozenset(
    "area base br col embed hr img input link meta param source track wbr".split()
)

#: Dropped tags that are *void*: they never have a closing tag, so they must be
#: skipped on their own.  Treating them like ``<script>`` (drop everything until
#: the matching end tag) swallows the rest of the message - a real bug, because
#: mail HTML routinely carries ``<meta>`` and ``<link>`` outside ``<head>``.
DROP_VOID: frozenset[str] = DROP_WITH_CONTENT & VOID_TAGS

#: Attributes accepted on every tag.
GLOBAL_ATTRS: frozenset[str] = frozenset({"style", "title", "dir", "lang", "align"})

#: Extra attributes, per tag.
TAG_ATTRS: dict[str, frozenset[str]] = {
    "a": frozenset({"href", "name"}),
    "img": frozenset({"src", "alt", "width", "height", "border", "hspace", "vspace"}),
    "table": frozenset({"border", "cellpadding", "cellspacing", "width", "height",
                        "bgcolor", "background", "summary"}),
    "td": frozenset({"colspan", "rowspan", "valign", "width", "height", "bgcolor", "nowrap"}),
    "th": frozenset({"colspan", "rowspan", "valign", "width", "height", "bgcolor", "nowrap"}),
    "tr": frozenset({"valign", "bgcolor", "height"}),
    "col": frozenset({"span", "width"}),
    "colgroup": frozenset({"span", "width"}),
    "font": frozenset({"color", "face", "size"}),
    "ol": frozenset({"start", "type", "reversed"}),
    "ul": frozenset({"type"}),
    "li": frozenset({"value", "type"}),
    "div": frozenset({"background"}),
    "blockquote": frozenset({"cite", "type"}),
    "hr": frozenset({"width", "size", "color", "noshade"}),
}

SAFE_LINK_SCHEMES: tuple[str, ...] = ("http://", "https://", "mailto:", "tel:", "ftp://",
                                      "ftps://", "sms:", "callto:", "webcal:", "news:")

#: Rejected inside a `style` attribute - the classic CSS attack surface.
_CSS_BLOCKLIST = re.compile(
    r"(expression\s*\(|javascript\s*:|vbscript\s*:|behavior\s*:|-moz-binding|"
    r"@import|position\s*:\s*fixed|position\s*:\s*absolute)",
    re.IGNORECASE,
)
_CSS_URL = re.compile(r"url\s*\(\s*['\"]?([^)'\"]*)['\"]?\s*\)", re.IGNORECASE)

#: Top level domains recognised in text that has no scheme, e.g.
#: "crowdworks.jp/public/jobs".  A list keeps "version 1.2.3" and "e.g." from
#: turning into links, which a generic "word.word" pattern would do.
_LINKABLE_TLDS = (
    "com|net|org|edu|gov|mil|int|info|biz|name|pro|app|dev|io|ai|me|co|tv|cc|"
    "shop|site|online|xyz|blog|cloud|tech|store|news|email|jp|kr|cn|tw|hk|sg|"
    "uk|de|fr|it|es|nl|se|no|fi|dk|pl|ru|ua|br|mx|ca|au|nz|in|id|th|vn|ph|ch|at"
)
_URL_IN_TEXT = re.compile(
    r"(?<![\w@.\-/])("
    r"(?:https?://|www\.)[^\s<>\"')\]]+"                     # explicit
    r"|(?:[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?\.)+"              # host labels
    rf"(?:{_LINKABLE_TLDS})"                                 # known TLD
    r"(?::\d{2,5})?(?:/[^\s<>\"')\]]*)?"                     # port and path
    r")",
    re.IGNORECASE,
)
_EMAIL_IN_TEXT = re.compile(r"(?<![\w.])([\w.+-]+@[\w-]+\.[\w.-]+)(?![\w.])")
_TRAILING_PUNCT = ".,;:!?、。）」"

#: `src` fragments that are tracking beacons even at a visible size.
_TRACKER_HINTS = ("/open?", "open.aspx", "/track", "tracking", "/pixel", "beacon",
                  "utm_medium=email&utm_", "/wf/open", "email_open", "/o/", "/imp?")


@dataclass
class SanitizedHtml:
    """Result of :func:`sanitize_html`."""

    html: str
    remote_images_blocked: int = 0
    remote_images_shown: int = 0
    trackers_removed: int = 0
    missing_inline_images: list[str] = field(default_factory=list)

    @property
    def has_blocked_content(self) -> bool:
        return self.remote_images_blocked > 0


#: ``cid`` (without angle brackets) -> ``data:`` URI, or ``None`` if unavailable.
CidResolver = Callable[[str], Optional[str]]


class _Sanitizer(HTMLParser):
    def __init__(self, cid_resolver: Optional[CidResolver], allow_remote_images: bool) -> None:
        super().__init__(convert_charrefs=True)
        self._cid_resolver = cid_resolver
        self._allow_remote = allow_remote_images
        self._out: list[str] = []
        self._open: list[str] = []
        self._skip_tag: Optional[str] = None
        self._skip_level = 0
        self.result = SanitizedHtml(html="")

    # ------------------------------------------------------------- HTMLParser
    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if self._skip_tag is not None:
            if tag == self._skip_tag:
                self._skip_level += 1
            return
        if tag in DROP_VOID:
            return                      # drop the tag, keep what follows
        if tag in DROP_WITH_CONTENT:
            self._skip_tag, self._skip_level = tag, 1
            return
        if tag == "body" or tag == "html":
            return  # unwrap: we build our own document shell
        if tag not in ALLOWED_TAGS:
            return  # unwrap unknown tag, keep its children

        if tag == "img":
            self._emit_image(dict((k.lower(), v or "") for k, v in attrs))
            return

        rendered = self._render_attrs(tag, attrs)
        if tag in VOID_TAGS:
            self._out.append(f"<{tag}{rendered}>")
        else:
            self._out.append(f"<{tag}{rendered}>")
            self._open.append(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip_tag is not None:
            if tag == self._skip_tag:
                self._skip_level -= 1
                if self._skip_level <= 0:
                    self._skip_tag = None
            return
        if tag in VOID_TAGS or tag not in ALLOWED_TAGS:
            return
        if tag not in self._open:
            return  # stray closing tag
        # Close everything the sender forgot to close, innermost first.
        while self._open:
            current = self._open.pop()
            self._out.append(f"</{current}>")
            if current == tag:
                break

    def handle_data(self, data: str) -> None:
        if self._skip_tag is not None or not data:
            return
        escaped = html.escape(data, quote=False)
        # Bare URLs written as text become clickable too - but never inside an
        # existing <a>, which would nest anchors.
        if "a" not in self._open:
            escaped = _linkify(escaped)
        self._out.append(escaped)

    def handle_entityref(self, name: str) -> None:  # convert_charrefs misses some
        if self._skip_tag is None:
            self._out.append(html.escape(html.unescape(f"&{name};"), quote=False))

    def handle_charref(self, name: str) -> None:
        if self._skip_tag is None:
            self._out.append(html.escape(html.unescape(f"&#{name};"), quote=False))

    def handle_comment(self, data: str) -> None:
        return  # comments can hide conditional markup; drop them

    def unknown_decl(self, data: str) -> None:
        return

    def error(self, message: str) -> None:  # pragma: no cover - py<3.10 hook
        logger.debug("HTML parse error: %s", message)

    # ----------------------------------------------------------------- output
    @property
    def unterminated(self) -> Optional[str]:
        """The tag we were still skipping when the input ended, if any."""
        return self._skip_tag

    def close_document(self) -> str:
        while self._open:
            self._out.append(f"</{self._open.pop()}>")
        return "".join(self._out)

    # ---------------------------------------------------------------- helpers
    def _render_attrs(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> str:
        allowed = GLOBAL_ATTRS | TAG_ATTRS.get(tag, frozenset())
        rendered: list[str] = []
        for raw_name, raw_value in attrs:
            name = (raw_name or "").lower()
            value = raw_value or ""
            if name.startswith("on") or name not in allowed:
                continue
            if name == "href":
                value = self._clean_link(value)
                if not value:
                    continue
            elif name == "style":
                value = _clean_style(value)
                if not value:
                    continue
            elif name == "background":
                continue  # remote background images are trackers too
            rendered.append(f' {name}="{html.escape(value, quote=True)}"')
        if tag == "a" and any(a.strip().startswith("href=") for a in rendered):
            rendered.append(' target="_blank" rel="noopener noreferrer"')
        return "".join(rendered)

    def _clean_link(self, href: str) -> str:
        value = html.unescape(href).strip().replace("\n", "").replace("\r", "")
        lowered = value.lower()
        if lowered.startswith("#") or value.startswith("/"):
            return ""  # relative links have no meaning outside the sender's site
        if lowered.startswith(SAFE_LINK_SCHEMES):
            return value
        return ""

    def _emit_image(self, attrs: dict[str, str]) -> None:
        src = html.unescape(attrs.get("src", "")).strip()
        alt = attrs.get("alt", "")
        if not src:
            return
        if _is_tracking_pixel(attrs, src):
            self.result.trackers_removed += 1
            return

        lowered = src.lower()
        if lowered.startswith("cid:"):
            cid = src[4:].strip().strip("<>")
            resolved = self._cid_resolver(cid) if self._cid_resolver else None
            if resolved is None:
                self.result.missing_inline_images.append(cid)
                self._out.append(
                    '<span class="inline-image-missing">[inline image: '
                    f"{html.escape(alt or cid, quote=False)}]</span>"
                )
                return
            src = resolved
        elif lowered.startswith("data:image/"):
            pass  # self-contained, safe
        elif lowered.startswith(("http://", "https://")):
            if not self._allow_remote:
                self.result.remote_images_blocked += 1
                self._out.append(
                    '<span class="remote-image-blocked">[remote image'
                    + (f": {html.escape(alt, quote=False)}" if alt else "")
                    + "]</span>"
                )
                return
            self.result.remote_images_shown += 1
        else:
            return  # file:, javascript:, unknown schemes

        rendered = [f' src="{html.escape(src, quote=True)}"']
        if alt:
            rendered.append(f' alt="{html.escape(alt, quote=True)}"')
        for name in ("width", "height"):
            value = attrs.get(name, "")
            if value and re.fullmatch(r"\d{1,5}%?", value.strip()):
                rendered.append(f' {name}="{html.escape(value.strip(), quote=True)}"')
        style = _clean_style(attrs.get("style", ""))
        if style:
            rendered.append(f' style="{html.escape(style, quote=True)}"')
        self._out.append("<img" + "".join(rendered) + ">")


def _clean_style(style: str) -> str:
    """Keep only declarations that cannot phone home or break the layout."""
    if not style:
        return ""
    value = html.unescape(style)
    kept: list[str] = []
    for declaration in value.split(";"):
        declaration = declaration.strip()
        if not declaration or ":" not in declaration:
            continue
        if _CSS_BLOCKLIST.search(declaration):
            continue
        urls = _CSS_URL.findall(declaration)
        if urls and not all(u.lower().startswith("data:image/") for u in urls):
            continue
        kept.append(declaration)
    return "; ".join(kept)


def _is_tracking_pixel(attrs: dict[str, str], src: str) -> bool:
    """1x1 beacons, zero-sized images and well known open-tracking URLs."""
    for name in ("width", "height"):
        raw = attrs.get(name, "").strip().rstrip("px").strip()
        if raw.isdigit() and int(raw) <= 1:
            return True
    style = attrs.get("style", "").lower().replace(" ", "")
    for prop in ("width:", "height:", "max-width:", "max-height:"):
        match = re.search(re.escape(prop) + r"(\d+)(px)?", style)
        if match and int(match.group(1)) <= 1:
            return True
    if "display:none" in style or "visibility:hidden" in style:
        return True
    lowered = src.lower()
    if lowered.startswith(("http://", "https://")) and any(h in lowered for h in _TRACKER_HINTS):
        return True
    return False


def sanitize_html(
    raw_html: str,
    cid_resolver: Optional[CidResolver] = None,
    allow_remote_images: bool = False,
) -> SanitizedHtml:
    """Sanitise a message body.  Never raises - worst case you get plain text."""
    if not raw_html:
        return SanitizedHtml(html="")
    parser = _Sanitizer(cid_resolver, allow_remote_images)
    try:
        parser.feed(raw_html)
        parser.close()
    except Exception:
        logger.warning("HTML sanitising failed, falling back to text", exc_info=True)
        return SanitizedHtml(html=text_to_html(html_to_text(raw_html)))
    result = parser.result
    result.html = parser.close_document()

    if parser.unterminated:
        logger.warning("HTML ended inside <%s>; the rest of the message was skipped",
                       parser.unterminated)

    # Safety net: if sanitising produced markup with no readable text at all
    # while the source clearly had some, show the text rather than a blank
    # panel.  An unclosed <style> or a malformed construct must never make a
    # message look empty.
    if not html_to_text(result.html).strip():
        fallback = html_to_text(raw_html).strip()
        if fallback:
            logger.warning("Sanitised HTML lost every text node; showing plain text "
                           "instead (%d characters recovered)", len(fallback))
            result.html = text_to_html(fallback)
    return result


# ------------------------------------------------------------------ plain text
def text_to_html(text: str) -> str:
    """Render a ``text/plain`` body as HTML, keeping layout and quoting."""
    if not text:
        return ""
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        quote_level = 0
        probe = line
        while probe.startswith(">") or probe.startswith(" >"):
            quote_level += 1
            probe = probe.lstrip(" ")[1:]
        content = _linkify(html.escape(probe if quote_level else line, quote=False))
        if quote_level:
            lines.append(f'<div class="quote quote-{min(quote_level, 3)}">{content}</div>')
        else:
            lines.append(f"<div>{content or '<br>'}</div>")
    return f'<div class="plain-body">{"".join(lines)}</div>'


def _linkify(escaped_text: str) -> str:
    """Turn bare URLs / addresses in *already escaped* text into links."""

    def url_repl(match: re.Match[str]) -> str:
        url = match.group(1)
        trailing = ""
        while url and url[-1] in _TRAILING_PUNCT:
            trailing, url = url[-1] + trailing, url[:-1]
        if not url:
            return match.group(0)
        # A URL written without a scheme gets https, not http: plain http would
        # be downgraded or refused by most of the sites people link to today.
        href = url if url.lower().startswith(("http://", "https://")) else "https://" + url
        return (f'<a href="{html.escape(href, quote=True)}" target="_blank" '
                f'rel="noopener noreferrer">{url}</a>{trailing}')

    def mail_repl(match: re.Match[str]) -> str:
        addr = match.group(1)
        return f'<a href="mailto:{html.escape(addr, quote=True)}">{addr}</a>'

    return _EMAIL_IN_TEXT.sub(mail_repl, _URL_IN_TEXT.sub(url_repl, escaped_text))


class _TextExtractor(HTMLParser):
    """HTML -> readable plain text (previews, search index, HTML-only mails)."""

    _BLOCK = frozenset("p div br tr li h1 h2 h3 h4 h5 h6 blockquote pre table "
                       "section article header footer hr".split())

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag in DROP_VOID:
            return                      # no closing tag: never start skipping
        if tag in DROP_WITH_CONTENT:
            self._skip += 1
        elif tag == "li":
            self.chunks.append("\n- ")
        elif tag in self._BLOCK:
            self.chunks.append("\n")
        elif tag == "img":
            alt = dict((k.lower(), v or "") for k, v in attrs).get("alt", "")
            if alt:
                self.chunks.append(f"[{alt}]")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in DROP_VOID:
            return
        if tag in DROP_WITH_CONTENT:
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCK:
            self.chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.chunks.append(data)


def html_to_text(raw_html: str) -> str:
    """Best-effort text rendering of HTML.  Used for previews and searching."""
    if not raw_html:
        return ""
    extractor = _TextExtractor()
    try:
        extractor.feed(raw_html)
        extractor.close()
        text = "".join(extractor.chunks)
    except Exception:
        logger.debug("html_to_text failed, stripping tags with a regex", exc_info=True)
        text = html.unescape(re.sub(r"<[^>]+>", " ", raw_html))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


# -------------------------------------------------------------------- document
_BASE_CSS = """
:root { color-scheme: light dark; }
html, body { margin: 0; padding: 0; }
body {
  font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  font-size: 14px; line-height: 1.5; color: %(fg)s; background: %(bg)s;
  padding: 16px; word-wrap: break-word; overflow-wrap: anywhere;
}
img { max-width: 100%%; height: auto; }
table { border-collapse: collapse; max-width: 100%%; }
table[border]:not([border="0"]) td, table[border]:not([border="0"]) th {
  border: 1px solid %(border)s; padding: 4px 8px;
}
a { color: %(link)s; }
pre, code, tt { font-family: Consolas, "SF Mono", Menlo, monospace;
  white-space: pre-wrap; background: %(soft)s; border-radius: 3px; padding: 1px 4px; }
pre { padding: 8px; }
blockquote { margin: 8px 0 8px 12px; padding-left: 12px;
  border-left: 3px solid %(border)s; color: %(muted)s; }
.plain-body { white-space: pre-wrap; font-family: inherit; }
.plain-body .quote { color: %(muted)s; border-left: 3px solid %(border)s; padding-left: 8px; }
.plain-body .quote-2 { opacity: .85; } .plain-body .quote-3 { opacity: .7; }
.remote-image-blocked, .inline-image-missing {
  display: inline-block; padding: 2px 8px; margin: 2px 0; font-size: 12px;
  color: %(muted)s; background: %(soft)s; border: 1px dashed %(border)s; border-radius: 4px;
}
"""

_LIGHT = {"fg": "#1f1f1f", "bg": "#ffffff", "border": "#dadce0",
          "link": "#1a73e8", "soft": "#f5f6f7", "muted": "#5f6368"}
_DARK = {"fg": "#e8eaed", "bg": "#1e1f22", "border": "#3c4043",
         "link": "#8ab4f8", "soft": "#2a2b2f", "muted": "#9aa0a6"}


def build_document(body_html: str, dark: bool = False) -> str:
    """Wrap a sanitised body in a styled, self-contained HTML document."""
    css = _BASE_CSS % (_DARK if dark else _LIGHT)
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"referrer\" content=\"no-referrer\">"
        f"<style>{css}</style></head><body>{body_html}</body></html>"
    )
