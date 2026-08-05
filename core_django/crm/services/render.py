"""Campaign template -> concrete subject and body for one contact.

Moved server-side from the local agent: the server owns the campaign template,
so an agent can never alter the wording of what goes out under its member's
name. Shares PLACEHOLDER_RE / ALLOWED_VARIABLES with campaigns.py so the
validation shown in the campaign form and the substitution done at send time
can never disagree.
"""

from dataclasses import dataclass

from .campaigns import ALLOWED_VARIABLES, PLACEHOLDER_RE


class MissingVariables(Exception):
    """A contact lacks data the template requires. Do not mail them."""

    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__("missing/blank variables: " + ", ".join(missing))


@dataclass
class Rendered:
    subject: str
    body: str


def contact_context(contact) -> dict[str, str]:
    return {
        "first_name": contact.first_name or "",
        "last_name": contact.last_name or "",
        "full_name": contact.full_name,
        "email": contact.email or "",
        "company": contact.company or "",
        "designation": contact.designation or "",
    }


def render(campaign, contact) -> Rendered:
    """Substitute every placeholder, or refuse.

    We raise rather than substituting an empty string: "Hi ," reads worse than
    a skipped contact, and a blank company in a subject line advertises that the
    mail was blasted. Callers surface these as MISSING_VARS so the data can be
    fixed before sending.
    """
    ctx = contact_context(contact)
    used = set(PLACEHOLDER_RE.findall(f"{campaign.mail_sub}\n{campaign.mail_body}"))

    unknown = used - ALLOWED_VARIABLES
    if unknown:
        raise MissingVariables(sorted(unknown))

    missing = sorted(v for v in used if not ctx.get(v, "").strip())
    if missing:
        raise MissingVariables(missing)

    def substitute(text: str) -> str:
        return PLACEHOLDER_RE.sub(lambda m: ctx[m.group(1)], text)

    return Rendered(subject=substitute(campaign.mail_sub), body=substitute(campaign.mail_body))
