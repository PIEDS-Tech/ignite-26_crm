"""The one place that understands campaign link syntax.

A lead writes `[book a call](https://cal.com/x)` in the plain body textarea and
the recipient gets clickable anchor text. Everything about that syntax lives
here -- the form's validation and the send-time conversion both import from
this module, exactly as they both import PLACEHOLDER_RE from campaigns.py, so
"what the form accepted" and "what went out" can never drift apart.

Why a markdown subset rather than a rich-text editor: we generate the HTML
ourselves from a syntax we control, so there is no untrusted HTML to sanitise
and no sanitiser dependency to keep current. That guarantee rests entirely on
to_html() escaping every piece it did not itself write. Do not weaken it.
"""

import re

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.utils.html import escape

#: `[label](url)`. The label forbids brackets so nesting cannot be attempted,
#: and the URL forbids whitespace so an unclosed paren fails to match rather
#: than swallowing the rest of the mail.
LINK_RE = re.compile(r"\[([^\[\]]+)\]\(\s*(\S+?)\s*\)")

#: mailto: is deliberately absent. Only schemes a mail client will open as a
#: web page are allowed; javascript: and data: are the reason this list exists.
ALLOWED_SCHEMES = ("http", "https")

_validate_url = URLValidator(schemes=list(ALLOWED_SCHEMES))

#: Inlined because mail clients strip <style> blocks. Kept deliberately plain --
#: this is a personal-looking outreach mail, not a newsletter.
_BODY_STYLE = (
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,"
    "Arial,sans-serif;font-size:14px;line-height:1.5;color:#111"
)


def extract_links(text: str) -> list[tuple[str, str]]:
    """Every (label, url) pair in the text, in order."""
    return LINK_RE.findall(text or "")


def validate_links(text: str) -> list[str]:
    """Problems with the links in `text`, as messages fit to show a user.

    Empty list means every link is sendable.
    """
    problems: list[str] = []

    for label, url in extract_links(text):
        if not label.strip():
            problems.append(f"A link has no text to click: [{label}]({url})")

        # Checked before URLValidator so `javascript:alert(1)` reports what is
        # actually wrong with it rather than a generic "enter a valid URL".
        scheme = url.split(":", 1)[0].lower() if ":" in url else ""
        if scheme and scheme not in ALLOWED_SCHEMES:
            problems.append(
                f"{url!r} uses the {scheme}: scheme. Links must start with http:// or https://."
            )
            continue

        # A placeholder standing in for the whole URL is resolved per contact at
        # send time, so there is nothing to validate here yet. render() escapes
        # whatever it substitutes, and a contact field is never a URL scheme we
        # allow, so the worst case is a dead link, not an unsafe one.
        if "{{" in url:
            continue

        try:
            _validate_url(url)
        except ValidationError:
            problems.append(f"{url!r} is not a valid URL.")

    return problems


def to_plain(text: str) -> str:
    """`[book a call](https://x)` -> `book a call (https://x)`.

    The text/plain alternative must not be lossy: a client that refuses HTML
    still has to be able to reach the link, so the URL stays visible.
    """
    return LINK_RE.sub(lambda m: f"{m.group(1).strip()} ({m.group(2)})", text or "")


def to_html(text: str) -> str:
    """Render the body as a full HTML document.

    Every run of ordinary text, every link label, and every href is escaped
    before it is interpolated. That is what makes this safe to mark |safe in
    the preview and to hand to Gmail without a sanitiser.
    """
    out: list[str] = []
    cursor = 0
    text = text or ""

    for match in LINK_RE.finditer(text):
        out.append(escape(text[cursor:match.start()]))
        label, url = match.group(1).strip(), match.group(2)
        out.append(f'<a href="{escape(url)}">{escape(label)}</a>')
        cursor = match.end()

    out.append(escape(text[cursor:]))

    # Escaping first and converting newlines second: the reverse would let a
    # <br> we just inserted be escaped back into visible text.
    body = "".join(out).replace("\n", "<br>\n")
    return (
        '<!doctype html><html><body style="' + _BODY_STYLE + '">\n'
        + body
        + "\n</body></html>"
    )
