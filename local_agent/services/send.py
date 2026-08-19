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


def _skip_status(skip: dict) -> str:
    """The outcome code for a skipped contact.

    The server now sends one. The substring matching below is the old path,
    kept only so a newer agent still works against a CRM that has not been
    updated yet -- and it is exactly why the code exists: rewording a server
    message used to turn "already mailed" into "failed" in this UI, silently.
    """
    code = skip.get("code")
    if code:
        return code

    lowered = (skip.get("reason") or "").lower()
    if "already has a mailing" in lowered:
        return ALREADY_MAILED
    if "not assigned" in lowered:
        return NOT_ASSIGNED
    if "missing" in lowered or "blank" in lowered:
        return MISSING_VARS
    return FAILED


#: Contacts reserved per round trip.
#:
#: This number is the difference between a bad afternoon and a wasted week.
#: Claiming a whole 400-contact selection up front commits a DRAFT row for every
#: one of them BEFORE a single mail is sent -- and a batch that size takes over
#: ten minutes to work through. When the connection died mid-batch (it did:
#: WinError 10053 on a Windows laptop, 19 Aug), 511 contacts were left claimed
#: but unsent, and a claimed contact cannot be re-claimed. The mails could not
#: simply be sent again.
#:
#: Claiming ten at a time bounds that damage to ten. Anything left over is
#: recoverable with "Resolve stranded drafts" instead of being a data-repair job.
CLAIM_CHUNK = 10


def _chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def send_batch(api, gmail, campaign_id: str, contact_ids: list[str], *,
               cc: str = "", bcc: str = "", delay=None, chunk_size=CLAIM_CHUNK):
    """Claim, send, report. Yields an Outcome per contact as it resolves.

    Works in small chunks: claim a few, send them, then claim the next few. The
    alternative -- one big claim up front -- means an interruption strands the
    entire remainder as unusable DRAFT rows.

    One failure never aborts the batch, and every claimed mailing gets a result
    reported even if its send blew up, otherwise it would sit as a stranded
    DRAFT for no reason.
    """
    delay = settings.send_delay_seconds if delay is None else delay
    remaining = len(contact_ids)

    for chunk in _chunks(list(contact_ids), max(1, chunk_size)):
        try:
            response = api.claim(campaign_id, chunk, cc=cc, bcc=bcc)
        except Exception as exc:
            # This chunk was never claimed, so nothing is stranded by it. Say so
            # per contact and stop: if the CRM is unreachable, the next chunk
            # will not fare better, and continuing would just produce noise.
            for cid in chunk:
                yield Outcome(contact_id=str(cid), email="", name="",
                              status=FAILED, detail=f"could not reserve: {exc}")
            return

        # Report everything the server refused before touching Gmail.
        for skip in response["skipped"]:
            yield Outcome(
                contact_id=skip["contact_id"],
                email=skip["email"],
                name=skip["name"],
                status=_skip_status(skip),
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
                    # The envelope is the server's word, not the laptop's: it
                    # validated these addresses and already recorded them on the
                    # DRAFT row before handing them back here.
                    from_name=item.get("from_name", ""),
                    cc=item.get("cc", ""),
                    bcc=item.get("bcc", ""),
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
                    # Worst case: mail sent, server not told. The DRAFT row is
                    # the evidence, and reconcile resolves it against Gmail.
                    yield Outcome(
                        **base, status=SENT,
                        detail=f"sent, but reporting failed ({exc}) — run reconcile",
                    )

            # Pace the batch. Gmail's per-account quota is real, and tripping it
            # throttles the mailbox for hours.
            remaining -= 1
            if delay and remaining > 0:
                time.sleep(delay)


def reconcile(api, gmail, max_rounds=20) -> list[Outcome]:
    """Resolve DRAFTs stranded by a crash between claim and report.

    Asks Gmail whether each mail actually went out, then tells the server. This
    is what makes the crash window safe rather than merely visible -- and it is
    the ONLY way a stranded draft becomes sendable again, because a draft that
    might already be in someone's inbox must never be assumed undelivered.

    Works in pages and keeps going until the queue is empty, because the batch
    that stranded them was large by definition. One unreadable draft is skipped
    rather than aborting the run: the whole point is to clear a backlog.
    """
    outcomes = []
    seen = set()

    for _ in range(max_rounds):
        try:
            page = api.drafts()
        except Exception as exc:                       # noqa: BLE001
            outcomes.append(Outcome("", "", "", FAILED, f"could not list drafts: {exc}"))
            break

        # Tolerate both shapes: the paged dict, and the bare list an older CRM
        # returns.
        drafts = page["drafts"] if isinstance(page, dict) else page
        fresh = [d for d in drafts if d["mailing_id"] not in seen]
        if not fresh:
            break

        for draft in fresh:
            seen.add(draft["mailing_id"])
            base = {
                "contact_id": draft["mailing_id"],
                "email": draft["to"],
                "name": draft["name"],
            }
            try:
                found = gmail.find_message_to(draft["to"], draft["subject"])
                if found:
                    api.report_sent(draft["mailing_id"], found.message_id, found.thread_id)
                    outcomes.append(Outcome(**base, status=SENT, detail="confirmed in Gmail"))
                else:
                    api.report_failed(
                        draft["mailing_id"], "stranded draft; no matching sent message"
                    )
                    outcomes.append(
                        Outcome(**base, status=FAILED, detail="marked failed, safe to retry")
                    )
            except Exception as exc:                   # noqa: BLE001
                # Leave it a DRAFT and move on; the next run picks it up again.
                outcomes.append(
                    Outcome(**base, status=FAILED, detail=f"could not resolve: {exc}")
                )

    return outcomes
