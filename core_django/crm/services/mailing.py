"""Claim / report: the transactional core of the whole system.

This is the code that guarantees a prospect is never mailed twice. It moved here
from the local agent when the agent stopped touching Postgres directly -- the
row lock and the unique constraint only mean something next to the database.

Read the ordering comments before changing anything.
"""

from dataclasses import dataclass
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from crm.models import Campaign, CampaignMailing, Contact
from shared.enums import (
    BLOCKED_LIFECYCLES,
    CampaignStatus,
    ContactLifecycle,
    MailingStatus,
)

from .render import MissingVariables, render

# Outcome codes shared with the API and both UIs.
OK = "OK"
ALREADY_MAILED = "ALREADY_MAILED"
NOT_ASSIGNED = "NOT_ASSIGNED"
MISSING_VARS = "MISSING_VARS"
CAP_REACHED = "CAP_REACHED"
ARCHIVED = "ARCHIVED"
BLOCKED = "BLOCKED"
SENT = "SENT"
FAILED = "FAILED"

#: Gmail's per-account quota is real; tripping it throttles the whole mailbox
#: for hours. Enforced server-side so it counts across every device a member uses.
DAILY_SEND_CAP = 400


class CampaignNotSendable(Exception):
    pass


@dataclass
class Skipped:
    contact_id: str
    email: str
    name: str
    reason: str


@dataclass
class Claimed:
    mailing_id: str
    contact_id: str
    to: str
    name: str
    subject: str
    body: str


def load_sendable_campaign(campaign_id) -> Campaign:
    try:
        campaign = Campaign.objects.get(id=campaign_id)
    except (Campaign.DoesNotExist, ValueError, TypeError):
        raise CampaignNotSendable(f"No campaign {campaign_id}")
    if campaign.status != CampaignStatus.ACTIVE.value:
        raise CampaignNotSendable(
            f"Campaign {campaign.title!r} is {campaign.status}; only "
            f"{CampaignStatus.ACTIVE.value} campaigns can be mailed."
        )
    return campaign


def sent_last_24h(member) -> int:
    since = timezone.now() - timedelta(hours=24)
    return CampaignMailing.objects.filter(
        sent_by=member, status=MailingStatus.SENT.value, sent_at__gte=since
    ).count()


def unmailable_reason(contact) -> tuple[str, str] | None:
    """Why this contact must not be mailed, as (outcome_code, human reason).

    Called from BOTH preflight and claim_batch so the dry run can never disagree
    with the real thing about who is sendable.
    """
    if contact.is_archived:
        return ARCHIVED, "archived"
    if contact.lifecycle in BLOCKED_LIFECYCLES:
        return BLOCKED, f"marked {contact.get_lifecycle_display().lower()}"
    return None


def preflight(campaign, member, contact_ids) -> list[dict]:
    """Dry run. Writes nothing; tells the user exactly what will happen."""
    already = set(
        CampaignMailing.objects.filter(
            campaign=campaign,
            contact_id__in=contact_ids,
            status__in=[MailingStatus.SENT.value, MailingStatus.DRAFT.value],
        ).values_list("contact_id", flat=True)
    )

    results = []
    for contact in Contact.objects.filter(id__in=contact_ids):
        base = {
            "contact_id": str(contact.id),
            "email": contact.email,
            "name": contact.full_name,
        }
        blocked = unmailable_reason(contact)

        if contact.assigned_to_id != member.id:
            results.append({**base, "status": NOT_ASSIGNED, "detail": "not assigned to you"})
        elif blocked:
            results.append({**base, "status": blocked[0], "detail": blocked[1]})
        elif contact.id in already:
            results.append({**base, "status": ALREADY_MAILED, "detail": "already has a mailing"})
        else:
            try:
                render(campaign, contact)
                results.append({**base, "status": OK, "detail": ""})
            except MissingVariables as exc:
                results.append({**base, "status": MISSING_VARS, "detail": str(exc)})
    return results


def claim_batch(campaign, member, contact_ids) -> tuple[list[Claimed], list[Skipped]]:
    """Reserve mailings for an agent to send.

    One transaction PER CONTACT, each committed before the agent is told about
    it. That ordering is the point: by the time a mail can leave the building,
    a durable DRAFT row already records the attempt. A crash can therefore
    leave an ambiguous DRAFT (resolvable) but never a sent mail with no record
    (unrecoverable).
    """
    claimed: list[Claimed] = []
    skipped: list[Skipped] = []

    budget = DAILY_SEND_CAP - sent_last_24h(member)

    for contact_id in contact_ids:
        if budget <= 0:
            skipped.append(
                Skipped(str(contact_id), "", "", f"daily cap of {DAILY_SEND_CAP} reached")
            )
            continue

        try:
            with transaction.atomic():
                # Lock the contact for the duration of the DB work only. The
                # Gmail round trip happens later, on the agent, with no lock
                # held -- otherwise one slow send would serialize the whole team.
                contact = Contact.objects.select_for_update().get(id=contact_id)

                if contact.assigned_to_id != member.id:
                    skipped.append(
                        Skipped(str(contact.id), contact.email, contact.full_name,
                                "not assigned to you")
                    )
                    continue

                # Re-checked here under the lock rather than trusted from the
                # agent's stale list: someone may have archived this contact
                # between the page loading and the send being pressed.
                blocked = unmailable_reason(contact)
                if blocked:
                    skipped.append(
                        Skipped(str(contact.id), contact.email, contact.full_name, blocked[1])
                    )
                    continue

                try:
                    rendered = render(campaign, contact)
                except MissingVariables as exc:
                    skipped.append(
                        Skipped(str(contact.id), contact.email, contact.full_name, str(exc))
                    )
                    continue

                # The unique constraint fires here for a duplicate. This is the
                # entire idempotency guarantee -- a double-click, a retry, or
                # two agents racing all collide at the database rather than
                # putting a second copy in a prospect's inbox.
                mailing = CampaignMailing.objects.create(
                    campaign=campaign,
                    contact=contact,
                    sent_by=member,
                    status=MailingStatus.DRAFT.value,
                    rendered_subject=rendered.subject,
                    rendered_body=rendered.body,
                )
        except Contact.DoesNotExist:
            skipped.append(Skipped(str(contact_id), "", "", "contact no longer exists"))
            continue
        except IntegrityError:
            contact = Contact.objects.filter(id=contact_id).first()
            skipped.append(
                Skipped(str(contact_id),
                        contact.email if contact else "",
                        contact.full_name if contact else "",
                        "already has a mailing")
            )
            continue

        budget -= 1
        claimed.append(
            Claimed(
                mailing_id=str(mailing.id),
                contact_id=str(contact.id),
                to=contact.email,
                name=contact.full_name,
                subject=rendered.subject,
                body=rendered.body,
            )
        )

    return claimed, skipped


@transaction.atomic
def record_result(mailing_id, member, *, status, message_id="", thread_id="", error="") -> dict:
    """Record what the agent's Gmail call actually did."""
    try:
        mailing = CampaignMailing.objects.select_for_update().get(id=mailing_id)
    except (CampaignMailing.DoesNotExist, ValueError, TypeError):
        return {"status": FAILED, "detail": "no such mailing"}

    if mailing.sent_by_id != member.id:
        return {"status": NOT_ASSIGNED, "detail": "not your mailing"}

    # Only a DRAFT is awaiting a result. Re-reporting a settled mailing is a
    # replayed request, not a new send -- ignore it rather than overwriting.
    if mailing.status != MailingStatus.DRAFT.value:
        return {"status": mailing.status.upper(), "detail": "already settled"}

    now = timezone.now()

    if status == "sent":
        mailing.status = MailingStatus.SENT.value
        mailing.mail_message_id = message_id or ""
        mailing.mail_thread_id = thread_id or ""
        mailing.sent_at = now
        mailing.error_detail = ""
        mailing.save()

        Contact.objects.filter(id=mailing.contact_id).update(
            last_contacted_by=member, last_contacted_at=now, updated_at=now
        )

        # The "changes the moment they mail" rule. Scoped to NEW deliberately:
        # a contact someone has already marked REPLIED must never be dragged
        # backwards to CONTACTED by a later campaign.
        Contact.objects.filter(
            id=mailing.contact_id, lifecycle=ContactLifecycle.NEW.value
        ).update(lifecycle=ContactLifecycle.CONTACTED.value, updated_at=now)

        return {"status": SENT, "detail": thread_id}

    mailing.status = MailingStatus.FAILED.value
    mailing.error_detail = (error or "unknown error")[:2000]
    mailing.save(update_fields=["status", "error_detail", "updated_at"])
    return {"status": FAILED, "detail": mailing.error_detail}


def stranded_drafts(member):
    """DRAFT rows left by an agent that died between claim and report."""
    return (
        CampaignMailing.objects.filter(sent_by=member, status=MailingStatus.DRAFT.value)
        .select_related("contact", "campaign")
        .order_by("created_at")
    )


@transaction.atomic
def reset_for_retry(mailing_id, member) -> bool:
    """Put a FAILED mailing back into DRAFT so it can be claimed again.

    Retry works by UPDATING this row. Inserting a second one is impossible --
    the unique constraint forbids it -- which is exactly what makes retrying safe.
    """
    try:
        mailing = CampaignMailing.objects.select_for_update().get(
            id=mailing_id, sent_by=member, status=MailingStatus.FAILED.value
        )
    except CampaignMailing.DoesNotExist:
        return False
    mailing.status = MailingStatus.DRAFT.value
    mailing.error_detail = ""
    mailing.save(update_fields=["status", "error_detail", "updated_at"])
    return True
