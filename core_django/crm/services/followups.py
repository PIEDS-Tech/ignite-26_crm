"""Follow-ups: send a second campaign to whoever stayed silent.

Two halves, and the split matters.

The server decides WHO is owed a follow-up and queues it as an ordinary
ScheduledSend -- so everything the scheduler already guarantees (the sending
window, the daily cap, the unique constraint, cancellation) applies to a
follow-up without a line of new machinery.

The agent decides WHETHER SOMEONE REPLIED, because only it can read Gmail. It
reports the fact; it does not act on it. A laptop that could mark contacts
REPLIED on its own would be a laptop that could rewrite the funnel.

On lifecycle: shared/enums.py says NEW -> CONTACTED is the only automatic
transition, on the grounds that inferring funnel state is guesswork. A reply
sitting in the thread is not guesswork -- but that rule was deliberate, so
setting REPLIED stays opt-in per rule (FollowUpRule.mark_replied).
"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from crm.models import CampaignMailing, FollowUpRule, ScheduledSend
from shared.enums import (
    TERMINAL_SCHEDULE_STATUSES,
    CampaignStatus,
    ContactLifecycle,
    MailingStatus,
)

from . import scheduling

#: Stop re-reading threads forever. A prospect who has not answered in a month
#: is not about to, and every check is a Gmail API call.
REPLY_WATCH_DAYS = 30


def threads_to_check(member, limit=50, now=None):
    """Sent mailings whose Gmail thread is worth re-reading.

    Ordered oldest-checked-first so a large mailbox is worked through fairly
    rather than the same few rows being polled every minute.
    """
    now = now or timezone.now()
    cutoff = now - timedelta(days=REPLY_WATCH_DAYS)

    return (
        CampaignMailing.objects
        .filter(
            sent_by=member,
            status=MailingStatus.SENT.value,
            replied_at__isnull=True,
            sent_at__gte=cutoff,
        )
        .exclude(mail_thread_id="")
        # Only threads a live rule actually cares about; checking the rest would
        # spend Gmail quota to learn something nobody asked for.
        .filter(campaign__follow_up_rules__is_active=True)
        .select_related("contact", "campaign")
        .distinct()
        .order_by("reply_checked_at", "sent_at")[:limit]
    )


@transaction.atomic
def record_reply_scan(mailing_id, member, *, replied: bool, now=None) -> dict:
    """Write down what the agent saw in one thread."""
    now = now or timezone.now()

    try:
        mailing = CampaignMailing.objects.select_for_update().select_related(
            "contact", "campaign"
        ).get(id=mailing_id, sent_by=member)
    except (CampaignMailing.DoesNotExist, ValueError, TypeError):
        return {"status": "unknown"}

    mailing.reply_checked_at = now
    fields = ["reply_checked_at", "updated_at"]

    if replied and mailing.replied_at is None:
        mailing.replied_at = now
        fields.append("replied_at")

        # Opt-in, per rule. See the module docstring.
        if FollowUpRule.objects.filter(
            campaign=mailing.campaign, is_active=True, mark_replied=True
        ).exists():
            # Never drag a contact backwards: someone hand-marked BOUNCED or
            # DO_NOT_CONTACT outranks anything we infer from a thread.
            from crm.models import Contact
            Contact.objects.filter(
                id=mailing.contact_id,
                lifecycle__in=[ContactLifecycle.NEW.value, ContactLifecycle.CONTACTED.value],
            ).update(lifecycle=ContactLifecycle.REPLIED.value, updated_at=now)

    mailing.save(update_fields=fields)
    return {"status": "replied" if mailing.replied_at else "no_reply"}


def due_for_follow_up(rule, now=None):
    """Mailings under `rule` that have gone unanswered long enough."""
    now = now or timezone.now()
    cutoff = now - timedelta(days=rule.delay_days)

    return (
        CampaignMailing.objects
        .filter(
            campaign=rule.campaign,
            status=MailingStatus.SENT.value,
            replied_at__isnull=True,
            followed_up_at__isnull=True,
            sent_at__lte=cutoff,
        )
        .select_related("contact", "sent_by")
    )


@transaction.atomic
def queue_follow_ups(rule, now=None) -> list:
    """Queue this rule's follow-up for everyone still silent.

    One ScheduledSend per original sender, not one per contact: the follow-up
    must leave the same mailbox as the mail it is chasing, or it arrives from a
    stranger with no thread behind it.

    Deliberately queued rather than sent: it then inherits the window, the cap,
    the cancel button and the lease from the scheduler, and there is exactly one
    code path that puts a scheduled mail in front of a prospect.
    """
    now = now or timezone.now()

    if not rule.is_active or rule.follow_up.status != CampaignStatus.ACTIVE.value:
        return []

    by_sender: dict = {}
    for mailing in due_for_follow_up(rule, now):
        by_sender.setdefault(mailing.sent_by, []).append(mailing)

    created = []
    for sender, mailings in by_sender.items():
        if not sender.is_active:
            continue

        # A contact already mailed under the follow-up campaign would be
        # refused by uniq_campaign_contact anyway; filtering here keeps the job
        # honest about its own size instead of reporting a batch of skips.
        already = set(
            CampaignMailing.objects.filter(
                campaign=rule.follow_up,
                contact_id__in=[m.contact_id for m in mailings],
            ).values_list("contact_id", flat=True)
        )
        contact_ids = [m.contact_id for m in mailings if m.contact_id not in already]
        if not contact_ids:
            continue

        job = scheduling.create(
            campaign_id=rule.follow_up_id,
            member=sender,
            contact_ids=contact_ids,
            # `now` and not "in a minute": the scheduler's own window decides
            # when this actually goes out, and it is the only thing that should.
            scheduled_at=now,
            created_by=rule.created_by,
            now=now,
        )
        created.append(job)

        # Stamped whether or not the mail eventually sends. This is "we have
        # queued a follow-up for this mailing", not "it arrived" -- without it
        # the next scan would queue a second one.
        CampaignMailing.objects.filter(
            id__in=[m.id for m in mailings if m.contact_id in set(contact_ids)]
        ).update(followed_up_at=now, updated_at=now)

    return created


def run_all_rules(now=None) -> list:
    """Every active rule. Called from the claim endpoint, like the other sweeps."""
    now = now or timezone.now()
    jobs = []
    for rule in FollowUpRule.objects.filter(is_active=True).select_related(
        "campaign", "follow_up", "created_by"
    ):
        try:
            jobs.extend(queue_follow_ups(rule, now))
        except scheduling.NotSchedulable:
            # A follow-up campaign that has been paused, or a rule whose
            # contacts have all gone. Not an error worth failing a poll over.
            continue
    return jobs


def cancel_pending_for(contact_id, campaign_id) -> int:
    """Pull a contact out of any queued follow-up they no longer deserve.

    Called when a reply lands after the follow-up was queued but before it went
    out -- the one window where we would otherwise chase someone who has
    already answered.

    Everything is normalised through str() because ArrayField(UUIDField) is
    asymmetric: ids go in as strings and come back as UUID objects, so a plain
    `in` test between the two forms quietly matches nothing.
    """
    key = str(contact_id)
    removed = 0

    for job in ScheduledSend.objects.filter(campaign_id=campaign_id).exclude(
        status__in=TERMINAL_SCHEDULE_STATUSES
    ):
        ids = [str(c) for c in job.contact_ids]
        # Only the part not yet attempted; rewriting sent history would be a lie.
        head, tail = ids[:job.cursor], ids[job.cursor:]
        if key not in tail:
            continue
        job.contact_ids = head + [c for c in tail if c != key]
        job.save(update_fields=["contact_ids", "updated_at"])
        removed += 1
    return removed
