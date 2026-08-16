"""The agent's send loop.

All the transactional safety now lives on the server (see
core_django/crm/services/mailing.py). What remains here is genuinely simple:
claim from the CRM, send through the member's own Gmail, report back.

That simplicity is the point. The laptop cannot corrupt shared state -- the
worst a crashed agent can do is leave a DRAFT the server already knows about.
"""

import time
from dataclasses import asdict, dataclass

from local_agent.config import settings

# Outcome codes, mirrored from crm/services/mailing.py.
SENT = "SENT"
FAILED = "FAILED"
ALREADY_MAILED = "ALREADY_MAILED"
NOT_ASSIGNED = "NOT_ASSIGNED"
MISSING_VARS = "MISSING_VARS"


@dataclass
class Outcome:
    contact_id: str
    email: str
    name: str
    status: str
    detail: str = ""

    def dict(self):
        return asdict(self)


def _skip_status(reason: str) -> str:
    """Map the server's prose reason onto a UI status code."""
    lowered = reason.lower()
    if "already has a mailing" in lowered:
        return ALREADY_MAILED
    if "not assigned" in lowered:
        return NOT_ASSIGNED
    if "missing" in lowered or "blank" in lowered:
        return MISSING_VARS
    return FAILED


def send_batch(api, gmail, campaign_id: str, contact_ids: list[str], *, delay=None):
    """Claim, send, report. Yields an Outcome per contact as it resolves.

    One failure never aborts the batch, and every claimed mailing gets a result
    reported even if its send blew up -- otherwise it would sit as a stranded
    DRAFT for no reason.
    """
    delay = settings.send_delay_seconds if delay is None else delay

    response = api.claim(campaign_id, contact_ids)

    # Report everything the server refused before touching Gmail.
    for skip in response["skipped"]:
        yield Outcome(
            contact_id=skip["contact_id"],
            email=skip["email"],
            name=skip["name"],
            status=_skip_status(skip["reason"]),
            detail=skip["reason"],
        )

    claimed = response["claimed"]
    for index, item in enumerate(claimed):
        base = {
            "contact_id": item["contact_id"],
            "email": item["to"],
            "name": item["name"],
        }

        try:
            # .get for body_html, not [...]: a laptop running an older agent
            # against an updated server then sends plain text instead of
            # crashing the whole batch on a KeyError.
            result = gmail.send(
                to=item["to"],
                subject=item["subject"],
                body=item["body"],
                body_html=item.get("body_html", ""),
            )
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            try:
                api.report_failed(item["mailing_id"], detail)
            except Exception:
                # The mail definitely did not go out; the DRAFT stays and
                # "Resolve stranded drafts" will clear it later.
                pass
            yield Outcome(**base, status=FAILED, detail=detail)
        else:
            try:
                api.report_sent(item["mailing_id"], result.message_id, result.thread_id)
                yield Outcome(**base, status=SENT, detail=result.thread_id)
            except Exception as exc:
                # Worst case: mail sent, server not told. The DRAFT row is the
                # evidence, and reconcile resolves it against Gmail.
                yield Outcome(
                    **base, status=SENT,
                    detail=f"sent, but reporting failed ({exc}) — run reconcile",
                )

        # Pace the batch. Gmail's per-account quota is real, and tripping it
        # throttles the mailbox for hours.
        if delay and index < len(claimed) - 1:
            time.sleep(delay)


def reconcile(api, gmail) -> list[Outcome]:
    """Resolve DRAFTs stranded by a crash between claim and report.

    Asks Gmail whether each mail actually went out, then tells the server. This
    is what makes the crash window safe rather than merely visible.
    """
    outcomes = []

    for draft in api.drafts():
        base = {
            "contact_id": draft["mailing_id"],
            "email": draft["to"],
            "name": draft["name"],
        }
        found = gmail.find_message_to(draft["to"], draft["subject"])

        if found:
            api.report_sent(draft["mailing_id"], found.message_id, found.thread_id)
            outcomes.append(Outcome(**base, status=SENT, detail="confirmed in Gmail"))
        else:
            api.report_failed(draft["mailing_id"], "stranded draft; no matching sent message")
            outcomes.append(Outcome(**base, status=FAILED, detail="marked failed, safe to retry"))

    return outcomes
