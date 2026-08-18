"""The background loop that makes a scheduled send actually send.

This is the process the whole feature rests on. Gmail has no `sendAt`, so
"9am Tuesday" only means anything if something is awake at 9am Tuesday holding
this member's Gmail token. That something is this loop.

It is deliberately thin. It does not decide who is due, whether a campaign is
still active, or who gets copied -- the server owns all of that and hands back
a slice of contact ids. This file asks "anything for me?", sends it with the
existing send_batch, and reports what happened. A laptop that disagrees with
the server about any of the above is a laptop that mails the wrong people.
"""

import asyncio
import logging
import os
import socket

from local_agent.api_client import ApiError
from local_agent.services.send import SENT, send_batch

log = logging.getLogger("ignite.schedule")

#: How often to ask the server for due work. Sixty seconds is well inside the
#: minute-level precision anyone schedules mail with, and 1440 polls a day is
#: nothing next to what the contact list already costs.
POLL_SECONDS = int(os.getenv("AGENT_SCHEDULE_POLL_SECONDS", "60"))

#: Jobs to lease per tick. A cap, not a target -- the rest wait one minute.
CLAIM_LIMIT = int(os.getenv("AGENT_SCHEDULE_CLAIM_LIMIT", "5"))


def agent_id() -> str:
    """Who holds the lease. Only ever read by a human reading the CRM."""
    return os.getenv("AGENT_ID") or f"{socket.gethostname()}:{os.getpid()}"


def run_job(api, gmail, job) -> dict:
    """Send one claimed job's slice. Returns what to report back.

    Counts `attempted`, not just sent: the server advances its cursor by that,
    and a contact who was permanently skipped (unassigned, archived,
    do_not_contact) must move the cursor too, or the job never finishes.
    """
    contact_ids = job.get("contact_ids") or []
    if not contact_ids:
        return {"attempted": 0, "sent": 0, "skipped": 0, "error": ""}

    sent = skipped = 0
    errors: list[str] = []

    for outcome in send_batch(
        api, gmail, job["campaign_id"], contact_ids,
        cc=job.get("cc", ""), bcc=job.get("bcc", ""),
    ):
        if outcome.status == SENT:
            sent += 1
        else:
            skipped += 1
            if outcome.detail:
                errors.append(f"{outcome.email}: {outcome.detail}")

    return {
        # Everything in the slice was resolved one way or the other -- claim_batch
        # returns a claim or a skip for each, and send_batch yields one outcome
        # per contact either way.
        "attempted": len(contact_ids),
        "sent": sent,
        "skipped": skipped,
        # Only a sample: a batch of 200 bad addresses should not post 200 lines
        # of prose into a text column someone has to read.
        "error": "; ".join(errors[:5])[:2000],
    }


def scan_replies(api, gmail) -> int:
    """Ask Gmail who wrote back, and tell the server.

    Runs before claiming work, deliberately: a reply noticed now can pull that
    contact out of a follow-up that is about to go out this very tick.

    Reports the fact and nothing else. Deciding what a reply MEANS -- whether it
    cancels a follow-up, whether it moves the contact's lifecycle -- is the
    server's, because that is shared state and this is one laptop.
    """
    checked = 0
    for row in api.reply_scan():
        try:
            replied = gmail.thread_has_reply_from(row["thread_id"], row["email"])
            api.report_reply(row["mailing_id"], replied)
            checked += 1
            if replied:
                log.info("reply seen from %s (%s)", row["email"], row["campaign"])
        except ApiError as exc:
            log.warning("could not report reply scan for %s: %s", row["mailing_id"], exc)
        except Exception:                                # noqa: BLE001
            # One unreadable thread must not stop the scan. It stays unchecked
            # and comes round again next tick.
            log.exception("reply scan failed for %s", row["mailing_id"])
    return checked


def tick(api, gmail) -> int:
    """One poll. Returns how many jobs were executed.

    Every failure mode here is per-job: one broken campaign must not stop the
    others, and must not stop the next tick either.
    """
    try:
        scan_replies(api, gmail)
    except ApiError as exc:
        # Follow-ups are a nicety; scheduled sends are not. A failing scan must
        # never stop the queue being drained.
        log.warning("reply scan skipped: %s", exc)

    response = api.claim_schedules(agent_id=agent_id(), limit=CLAIM_LIMIT)

    requeued = response.get("requeued_stale") or 0
    if requeued:
        log.warning("requeued %s scheduled send(s) stranded by a dead executor", requeued)

    jobs = response.get("claimed") or []
    for job in jobs:
        job_id = job["id"]
        try:
            result = run_job(api, gmail, job)
        except Exception as exc:                     # noqa: BLE001 - see below
            # A job-level failure: Gmail auth gone, the CRM unreachable
            # mid-batch. Individual bad mails never reach here -- send_batch
            # already isolates those and reports them per contact.
            log.exception("scheduled send %s failed", job_id)
            try:
                api.report_schedule_failed(job_id, f"{type(exc).__name__}: {exc}")
            except ApiError:
                # Could not even report it. The lease will expire and the
                # server will requeue the job; nothing is lost.
                log.error("could not report failure of %s; leaving the lease to expire", job_id)
            continue

        try:
            api.report_schedule_progress(job_id, **result)
            log.info(
                "scheduled send %s: %s sent, %s skipped (%s)",
                job_id, result["sent"], result["skipped"], job.get("campaign"),
            )
        except ApiError as exc:
            # Worst case: mail went out, the server was not told. Same shape as
            # a stranded DRAFT -- the mailing rows are the evidence, the lease
            # expires, and re-running is harmless because uniq_campaign_contact
            # refuses a second mail to anyone already done.
            log.error("sent %s but could not report progress: %s", job_id, exc)

    return len(jobs)


async def loop(get_api, get_gmail, send_lock, stop: asyncio.Event):
    """Poll forever. Started from main.py's lifespan.

    `send_lock` is a threading.Lock shared with the manual /api/send route --
    threading and not asyncio because that route is a plain `def` streaming
    response and runs in Starlette's threadpool, where an asyncio lock would be
    unacquirable. Both paths pace themselves between mails; letting them
    interleave would break that pacing and walk the mailbox towards Gmail's rate
    limit, which throttles it for hours.

    The blocking work runs in a thread: send_batch is synchronous and sleeps
    between mails, and doing that on the event loop would freeze the agent's UI
    for the length of a batch. The lock is taken INSIDE that thread, so a long
    manual send delays the next tick rather than blocking the event loop.
    """
    log.info("scheduler polling every %ss as %s", POLL_SECONDS, agent_id())

    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=POLL_SECONDS)
            break                                     # stop was set: shut down
        except asyncio.TimeoutError:
            pass                                      # the normal path: tick

        def _locked_tick():
            with send_lock:
                return tick(get_api(), get_gmail())

        try:
            await asyncio.to_thread(_locked_tick)
        except ApiError as exc:
            # The CRM is down or the token was revoked. Say so once and try
            # again next minute; a scheduler that exits on a blip is worse than
            # one that is briefly noisy.
            log.warning("scheduler poll failed: %s", exc)
        except Exception:                             # noqa: BLE001
            log.exception("scheduler poll crashed; continuing")
