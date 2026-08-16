"""JSON endpoints driving the local sending agents.

The agent holds exactly one thing this server cannot: the member's Gmail
credentials. Everything else -- who owns a contact, what the template says,
whether a mail already went out -- is decided here.
"""

from dataclasses import asdict

from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST

from crm.models import Campaign, CampaignMailing, Contact
from crm.services import contacts as contact_svc
from crm.services import mailing as mailing_svc
from shared.enums import CampaignStatus, MailingStatus

from .auth import api_token_required, json_error, parse_json


@require_GET
@api_token_required
def me(request):
    """Identity handshake. The agent aborts if this disagrees with its config."""
    member = request.member
    return JsonResponse({
        "id": str(member.id),
        "name": member.name,
        "bits_email": member.bits_email,
        "batch": member.batch,
        "assigned_contacts": member.assigned_contacts.count(),
        "sent_last_24h": mailing_svc.sent_last_24h(member),
        "daily_cap": mailing_svc.DAILY_SEND_CAP,
    })


@require_GET
@api_token_required
def campaigns(request):
    qs = Campaign.objects.filter(status=CampaignStatus.ACTIVE.value)
    return JsonResponse([
        {"id": str(c.id), "title": c.title, "mail_sub": c.mail_sub, "var_list": c.var_list}
        for c in qs
    ], safe=False)


@require_GET
@api_token_required
def contacts(request):
    """The member's assigned contacts, annotated for the selected campaign.

    `already_mailed` lets the UI grey a row out *before* the unique constraint
    has to reject it -- the constraint stays the real guarantee, this is just
    courtesy.
    """
    member = request.member
    campaign_id = request.GET.get("campaign_id")

    mailed = set()
    if campaign_id:
        mailed = set(
            CampaignMailing.objects.filter(
                campaign_id=campaign_id,
                status__in=[MailingStatus.SENT.value, MailingStatus.DRAFT.value],
            ).values_list("contact_id", flat=True)
        )

    # Archived contacts are excluded outright rather than shown greyed: they are
    # refused by claim_batch anyway, and offering them would only invite a
    # confusing skip at send time.
    qs = (
        Contact.objects.filter(assigned_to=member, is_archived=False)
        .order_by("company", "first_name")
    )
    return JsonResponse([_contact_json(c, mailed) for c in qs], safe=False)


def _contact_json(c, mailed=frozenset()):
    """One contact, with enough detail for the agent's inline edit dialog."""
    return {
        "id": str(c.id),
        "name": c.full_name,
        "first_name": c.first_name,
        "last_name": c.last_name,
        "email": c.email,
        "phone_no": c.phone_no,
        "linkedin": c.linkedin,
        "company": c.company,
        "designation": c.designation,
        "tags": list(c.tags or []),
        "lifecycle": c.lifecycle,
        "lifecycle_label": c.get_lifecycle_display(),
        "mailable": c.is_mailable,
        "already_mailed": c.id in mailed,
        "last_contacted_at": c.last_contacted_at.isoformat() if c.last_contacted_at else None,
    }


@api_token_required
def contact_create(request):
    """Add a contact from the agent. Always assigned to the calling member."""
    if request.method != "POST":
        return json_error("POST required.", status=405)

    payload, error = parse_json(request)
    if error:
        return error

    try:
        contact = contact_svc.create(payload, request.member)
    except DjangoValidationError as exc:
        return json_error("; ".join(_flatten(exc)), status=422)
    except PermissionDenied as exc:
        return json_error(str(exc), status=403)

    return JsonResponse(_contact_json(contact), status=201)


@api_token_required
def contact_update(request, contact_id):
    """Edit a contact from the agent's row dialog.

    Same permission rule as the web form -- a member may only change contacts
    assigned to them, and `lifecycle` is dropped for non-leads by the service.
    """
    if request.method not in ("PATCH", "POST"):
        return json_error("PATCH required.", status=405)

    try:
        contact = Contact.objects.get(id=contact_id)
    except (Contact.DoesNotExist, ValueError, TypeError):
        return json_error("No such contact.", status=404)

    payload, error = parse_json(request)
    if error:
        return error

    try:
        contact = contact_svc.update(contact, payload, request.member)
    except PermissionDenied as exc:
        return json_error(str(exc), status=403)
    except DjangoValidationError as exc:
        return json_error("; ".join(_flatten(exc)), status=422)

    return JsonResponse(_contact_json(contact))


def _flatten(exc) -> list[str]:
    """Django validation errors, field-labelled, as flat strings."""
    if hasattr(exc, "message_dict"):
        return [f"{f}: {' '.join(m)}" for f, m in exc.message_dict.items()]
    return list(exc.messages)


def _campaign_and_ids(request):
    """Shared validation for preflight and claim."""
    payload, error = parse_json(request)
    if error:
        return None, None, error

    contact_ids = payload.get("contact_ids") or []
    if not isinstance(contact_ids, list) or not contact_ids:
        return None, None, json_error("contact_ids must be a non-empty list.")

    try:
        campaign = mailing_svc.load_sendable_campaign(payload.get("campaign_id"))
    except mailing_svc.CampaignNotSendable as exc:
        return None, None, json_error(str(exc))

    return campaign, contact_ids, None


@require_POST
@api_token_required
def preflight(request):
    campaign, contact_ids, error = _campaign_and_ids(request)
    if error:
        return error

    member = request.member
    outcomes = mailing_svc.preflight(campaign, member, contact_ids)

    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o["status"]] = counts.get(o["status"], 0) + 1

    # Render the first sendable one so the operator sees a real mail, not a template.
    sample = next((o for o in outcomes if o["status"] == mailing_svc.OK), None)
    preview = None
    if sample:
        contact = Contact.objects.get(id=sample["contact_id"])
        rendered = mailing_svc.render(campaign, contact)
        preview = {
            "to": contact.email,
            "subject": rendered.subject,
            "body": rendered.body,
            "body_html": rendered.body_html,
        }

    return JsonResponse({
        "campaign": campaign.title,
        "counts": counts,
        "sendable": counts.get(mailing_svc.OK, 0),
        "remaining_today": mailing_svc.DAILY_SEND_CAP - mailing_svc.sent_last_24h(member),
        "preview": preview,
        "outcomes": outcomes,
    })


@require_POST
@api_token_required
def claim(request):
    """Reserve mailings. Every returned item has a committed DRAFT row."""
    campaign, contact_ids, error = _campaign_and_ids(request)
    if error:
        return error

    claimed, skipped = mailing_svc.claim_batch(campaign, request.member, contact_ids)
    return JsonResponse({
        "claimed": [asdict(c) for c in claimed],
        "skipped": [asdict(s) for s in skipped],
    })


@require_POST
@api_token_required
def report_result(request, mailing_id):
    payload, error = parse_json(request)
    if error:
        return error

    status = payload.get("status")
    if status not in ("sent", "failed"):
        return json_error("status must be 'sent' or 'failed'.")

    return JsonResponse(mailing_svc.record_result(
        mailing_id,
        request.member,
        status=status,
        message_id=payload.get("message_id", ""),
        thread_id=payload.get("thread_id", ""),
        error=payload.get("error", ""),
    ))


@require_GET
@api_token_required
def drafts(request):
    """DRAFTs stranded by an agent that died between claim and report."""
    return JsonResponse([
        {
            "mailing_id": str(m.id),
            "campaign": m.campaign.title,
            "to": m.contact.email,
            "name": m.contact.full_name,
            "subject": m.rendered_subject,
            "body": m.rendered_body,
            "body_html": m.rendered_body_html,
            "created_at": m.created_at.isoformat(),
        }
        for m in mailing_svc.stranded_drafts(request.member)
    ], safe=False)
