from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from shared.enums import CampaignStatus, ContactLifecycle, MailingStatus

from .forms import BulkEditForm, CampaignForm, ContactForm, CsvUploadForm, NoteForm, TokenForm
from .models import ApiToken, Campaign, CampaignMailing, Contact, TeamMember
from .services import assignment, importer
from .services import campaigns as campaign_svc
from .services import contacts as contact_svc
from .services.permissions import (
    can_edit_contact,
    can_hard_delete,
    is_lead,
    lead_required,
    member_required,
)
from .services.render import MissingVariables
from .services.render import render as render_mail   # not django.shortcuts.render

#: Preview payloads are held in the session between the upload and confirm
#: steps. Small enough for a session cookie backend and avoids a temp table.
IMPORT_SESSION_KEY = "pending_import"


def _base(request, **extra):
    return {"member": request.member, "is_lead": is_lead(request.member), **extra}


# ---------------------------------------------------------------- dashboard

@member_required
def home(request):
    funnels = (
        Campaign.objects.annotate(
            sent=Count("mailings", filter=Q(mailings__status=MailingStatus.SENT.value)),
            failed=Count("mailings", filter=Q(mailings__status=MailingStatus.FAILED.value)),
            drafted=Count("mailings", filter=Q(mailings__status=MailingStatus.DRAFT.value)),
        )
        .order_by("-created_at")[:6]
    )
    recent_failures = (
        CampaignMailing.objects.filter(status=MailingStatus.FAILED.value)
        .select_related("contact", "campaign", "sent_by")[:8]
    )
    return render(request, "crm/home.html", _base(
        request,
        my_contacts=request.member.assigned_contacts.count(),
        total_contacts=Contact.objects.count(),
        unassigned=Contact.objects.filter(assigned_to__isnull=True).count(),
        sent_total=CampaignMailing.objects.filter(status=MailingStatus.SENT.value).count(),
        funnels=funnels,
        recent_failures=recent_failures,
    ))


# ----------------------------------------------------------------- contacts

def _filtered_contacts(request):
    """Shared filtering for the contact list and the assignment screen."""
    qs = Contact.objects.select_related("assigned_to").order_by("company", "first_name")

    q = request.GET.get("q", "").strip()
    company = request.GET.get("company", "").strip()
    assignee = request.GET.get("assignee", "").strip()
    tag = request.GET.get("tag", "").strip()
    lifecycle = request.GET.get("lifecycle", "").strip()
    archived = request.GET.get("archived", "").strip()

    # Archived contacts are hidden everywhere unless explicitly asked for. They
    # are also refused by claim_batch, so this is presentation, not safety.
    qs = qs.filter(is_archived=True) if archived == "1" else qs.filter(is_archived=False)

    if q:
        qs = qs.filter(
            Q(first_name__icontains=q) | Q(last_name__icontains=q)
            | Q(email__icontains=q) | Q(company__icontains=q)
        )
    if company:
        qs = qs.filter(company=company)
    if tag:
        qs = qs.filter(tags__contains=[tag])
    if lifecycle:
        qs = qs.filter(lifecycle=lifecycle)
    if assignee == "unassigned":
        qs = qs.filter(assigned_to__isnull=True)
    elif assignee == "mine":
        qs = qs.filter(assigned_to=request.member)
    elif assignee:
        qs = qs.filter(assigned_to_id=assignee)

    return qs, {
        "q": q, "company": company, "assignee": assignee,
        "tag": tag, "lifecycle": lifecycle, "archived": archived,
    }


def _filter_context(request):
    qs, filters = _filtered_contacts(request)
    return qs, {
        "filters": filters,
        "companies": Contact.objects.filter(is_archived=False).order_by("company")
                            .values_list("company", flat=True).distinct(),
        "all_tags": contact_svc.all_tags(),
        "lifecycles": ContactLifecycle.choices(),
        "members": TeamMember.objects.filter(is_active=True)
                           .annotate(load=Count("assigned_contacts")),
    }


@member_required
def contact_list(request):
    qs, ctx = _filter_context(request)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "crm/contact_list.html", _base(
        request,
        page=page,
        total=qs.count(),
        bulk_form=BulkEditForm(actor=request.member),
        **ctx,
    ))


@member_required
def contact_detail(request, pk):
    contact = get_object_or_404(
        Contact.objects.select_related("assigned_to", "last_contacted_by"), pk=pk
    )

    if request.method == "POST":
        form = NoteForm(request.POST)
        if form.is_valid():
            note = form.save(commit=False)
            note.contact = contact
            note.author = request.member
            note.save()
            messages.success(request, "Note added.")
            return redirect("crm:contact_detail", pk=pk)
    else:
        form = NoteForm()

    return render(request, "crm/contact_detail.html", _base(
        request,
        contact=contact,
        form=form,
        notes=contact.notes.select_related("author"),
        mailings=contact.mailings.select_related("campaign", "sent_by"),
        audits=contact.audits.select_related("actor")[:50],
        can_edit=can_edit_contact(request.member, contact),
        can_delete=can_hard_delete(request.member, contact),
    ))


# ------------------------------------------------------------- contact CRUD
# @member_required, not @lead_required: a 2025 member may edit the contacts
# assigned to them. The per-object check is inside, via can_edit_contact.

@member_required
def contact_new(request):
    if request.method == "POST":
        form = ContactForm(request.POST, actor=request.member)
        if form.is_valid():
            data = dict(form.cleaned_data)
            data["tags"] = data.pop("tags_raw")
            try:
                contact = contact_svc.create(data, request.member)
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
            else:
                messages.success(request, f"Added {contact.full_name}.")
                return redirect("crm:contact_detail", pk=contact.pk)
    else:
        form = ContactForm(actor=request.member)

    return render(request, "crm/contact_form.html", _base(request, form=form, contact=None))


@member_required
def contact_edit(request, pk):
    contact = get_object_or_404(Contact, pk=pk)
    if not can_edit_contact(request.member, contact):
        raise PermissionDenied(
            "You can only edit contacts assigned to you. Ask a lead to reassign it."
        )

    if request.method == "POST":
        form = ContactForm(request.POST, instance=contact, actor=request.member)
        if form.is_valid():
            data = dict(form.cleaned_data)
            data["tags"] = data.pop("tags_raw")
            try:
                contact_svc.update(contact, data, request.member)
            except (ValidationError, PermissionDenied) as exc:
                messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
            else:
                messages.success(request, "Saved.")
                return redirect("crm:contact_detail", pk=contact.pk)
    else:
        form = ContactForm(instance=contact, actor=request.member)

    return render(request, "crm/contact_form.html", _base(request, form=form, contact=contact))


@member_required
@require_POST
def contact_archive(request, pk):
    contact = get_object_or_404(Contact, pk=pk)
    archive = request.POST.get("archived") != "0"
    try:
        contact_svc.set_archived(contact, request.member, archived=archive)
    except PermissionDenied as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f"{contact.full_name} archived — it will no longer receive mail."
            if archive else f"{contact.full_name} restored.",
        )
    return redirect(request.POST.get("next") or "crm:contact_list")


@lead_required
@require_POST
def contact_delete(request, pk):
    contact = get_object_or_404(Contact, pk=pk)
    try:
        email = contact_svc.hard_delete(contact, request.member)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("crm:contact_detail", pk=pk)
    except PermissionDenied as exc:
        messages.error(request, str(exc))
        return redirect("crm:contact_detail", pk=pk)

    messages.success(request, f"Deleted {email} permanently.")
    return redirect("crm:contact_list")


@member_required
@require_POST
def contact_bulk_edit(request):
    contact_ids = request.POST.getlist("contact_ids")
    redirect_to = request.POST.get("next") or "crm:contact_list"

    if not contact_ids:
        messages.error(request, "No contacts selected.")
        return redirect(redirect_to)

    form = BulkEditForm(request.POST, actor=request.member)
    if not form.is_valid():
        messages.error(request, "Check the bulk-edit fields.")
        return redirect(redirect_to)

    try:
        result = contact_svc.bulk_edit(
            contact_ids,
            request.member,
            company=form.cleaned_data.get("company"),
            designation=form.cleaned_data.get("designation"),
            tags_add=form.cleaned_data.get("tags_add"),
            tags_remove=form.cleaned_data.get("tags_remove"),
            lifecycle=form.cleaned_data.get("lifecycle"),
        )
    except PermissionDenied as exc:
        messages.error(request, str(exc))
        return redirect(redirect_to)

    if result.updated:
        messages.success(request, f"Updated {result.updated} contact(s).")
    if result.skipped:
        messages.error(
            request,
            f"Skipped {len(result.skipped)} not assigned to you.",
        )
    if not result.updated and not result.skipped:
        messages.info(request, "Nothing to change — the values already matched.")
    return redirect(redirect_to)


# --------------------------------------------------------------- assignment

@lead_required
def assign(request):
    qs, ctx = _filter_context(request)
    # annotate, NOT `{{ c.mailings.count }}` in the template: that was one extra
    # query per row -- 100 round trips to a hosted database, ~11s per render.
    # A page that slow is not just unpleasant, it broke selection outright (the
    # background refresh landed mid-click and rolled the ticks back).
    qs = qs.annotate(mailed=Count("mailings", distinct=True))
    page = Paginator(qs, 100).get_page(request.GET.get("page"))
    return render(request, "crm/assign.html", _base(request, page=page, total=qs.count(), **ctx))


@lead_required
@require_POST
def assign_apply(request):
    contact_ids = request.POST.getlist("contact_ids")
    action = request.POST.get("action")
    redirect_to = request.POST.get("next") or "crm:assign"

    if not contact_ids:
        messages.error(request, "No contacts selected.")
        return redirect(redirect_to)

    if action == "unassign":
        count = assignment.bulk_unassign(contact_ids)
        messages.success(request, f"Unassigned {count} contact(s).")
        return redirect(redirect_to)

    member = TeamMember.objects.filter(pk=request.POST.get("member")).first()
    if not member:
        messages.error(request, "Pick a team member to assign to.")
        return redirect(redirect_to)

    try:
        result = assignment.bulk_assign(
            contact_ids, member, force=request.POST.get("force") == "1"
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect(redirect_to)

    if result.assigned:
        messages.success(request, f"Assigned {result.assigned} contact(s) to {member.name}.")

    # bulk_assign refuses to move a contact that already has mail history under
    # someone else. Surface those rather than dropping them silently -- the
    # lead needs to decide, and can re-post with force.
    if result.skipped:
        detail = "; ".join(f"{email} ({reason})" for email, reason in result.skipped[:5])
        more = f" …and {len(result.skipped) - 5} more" if len(result.skipped) > 5 else ""
        messages.error(request, f"Skipped {len(result.skipped)}: {detail}{more}")

    return redirect(redirect_to)


# ------------------------------------------------------------------- import

@lead_required
def contact_import(request):
    preview = None

    if request.method == "POST":
        form = CsvUploadForm(request.POST, request.FILES)
        if form.is_valid():
            preview = importer.parse(form.cleaned_data["file"].read())
            if preview.ok:
                request.session[IMPORT_SESSION_KEY] = [
                    r.data for r in preview.importable
                ]
            else:
                messages.error(request, preview.header_error)
    else:
        form = CsvUploadForm()

    return render(request, "crm/contact_import.html", _base(
        request, form=form, preview=preview,
        NEW=importer.NEW, DUPLICATE=importer.DUPLICATE, INVALID=importer.INVALID,
    ))


@lead_required
@require_POST
def contact_import_confirm(request):
    rows = request.session.pop(IMPORT_SESSION_KEY, None)
    if not rows:
        messages.error(request, "Nothing pending — upload the file again.")
        return redirect("crm:contact_import")

    created = Contact.objects.bulk_create(
        [Contact(**data, created_by=request.member) for data in rows]
    )
    messages.success(request, f"Imported {len(created)} contact(s).")
    return redirect("crm:contact_list")


# ---------------------------------------------------------------- campaigns

@member_required
def campaign_list(request):
    campaigns = Campaign.objects.annotate(
        sent=Count("mailings", filter=Q(mailings__status=MailingStatus.SENT.value)),
        failed=Count("mailings", filter=Q(mailings__status=MailingStatus.FAILED.value)),
    )
    return render(request, "crm/campaign_list.html", _base(request, campaigns=campaigns))


@member_required
def campaign_detail(request, pk):
    campaign = get_object_or_404(Campaign, pk=pk)
    mailings = campaign.mailings.select_related("contact", "sent_by").order_by("-created_at")

    counts = {
        status: mailings.filter(status=status).count()
        for status in (MailingStatus.SENT.value, MailingStatus.DRAFT.value,
                       MailingStatus.FAILED.value)
    }

    # Render against a real assigned contact so the preview shows what a
    # recipient actually receives, not the raw template.
    sample = Contact.objects.filter(assigned_to__isnull=False).first()
    preview, preview_error = None, None
    if sample:
        try:
            preview = render_mail(campaign, sample)
        except MissingVariables as exc:
            preview_error = str(exc)

    return render(request, "crm/campaign_detail.html", _base(
        request, campaign=campaign, counts=counts,
        mailings=Paginator(mailings, 100).get_page(request.GET.get("page")),
        assigned_pool=Contact.objects.filter(assigned_to__isnull=False).count(),
        preview=preview, preview_error=preview_error, sample=sample,
        transitions=campaign_svc.ALLOWED_TRANSITIONS[CampaignStatus(campaign.status)],
    ))


@lead_required
def campaign_edit(request, pk=None):
    campaign = get_object_or_404(Campaign, pk=pk) if pk else None

    if request.method == "POST":
        form = CampaignForm(request.POST, instance=campaign)
        if form.is_valid():
            obj = form.save(commit=False)
            if campaign is None:
                obj.created_by = request.member
            obj.save()
            messages.success(request, "Campaign saved.")
            return redirect("crm:campaign_detail", pk=obj.pk)
    else:
        form = CampaignForm(instance=campaign)

    return render(request, "crm/campaign_form.html", _base(
        request, form=form, campaign=campaign,
    ))


@lead_required
@require_POST
def campaign_transition(request, pk):
    campaign = get_object_or_404(Campaign, pk=pk)
    try:
        campaign_svc.transition(campaign, request.POST.get("status"))
        messages.success(request, f"Campaign is now {campaign.status}.")
    except (ValidationError, ValueError) as exc:
        detail = "; ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
        messages.error(request, detail)
    return redirect("crm:campaign_detail", pk=pk)


# ------------------------------------------------------------ team & tokens

@lead_required
def member_list(request):
    form = TokenForm(members=TeamMember.objects.filter(is_active=True))
    return render(request, "crm/member_list.html", _base(
        request,
        members=TeamMember.objects.annotate(
            load=Count("assigned_contacts", distinct=True),
            sent=Count("mailings", filter=Q(mailings__status=MailingStatus.SENT.value),
                       distinct=True),
        ),
        tokens=ApiToken.objects.select_related("member"),
        form=form,
        new_token=request.session.pop("new_token", None),
    ))


@lead_required
@require_POST
def member_sender_name(request, pk):
    """Set what a member's recipients see in the From line.

    Lead-only: this is the name a cold prospect judges the mail by, so it is not
    something an individual member changes on their own laptop. Blank clears it
    and falls back to the member's real name.
    """
    member = get_object_or_404(TeamMember, pk=pk)
    member.sender_name = (request.POST.get("sender_name") or "").strip()[:120]
    member.save(update_fields=["sender_name", "updated_at"])
    messages.success(
        request, f"{member.name} now sends as “{member.display_name}”."
    )
    return redirect("crm:member_list")


@lead_required
@require_POST
def token_issue(request):
    form = TokenForm(request.POST, members=TeamMember.objects.filter(is_active=True))
    if not form.is_valid():
        messages.error(request, "Pick a team member.")
        return redirect("crm:member_list")

    _, raw = ApiToken.issue(form.cleaned_data["member"], form.cleaned_data["label"])
    # Shown exactly once -- only the hash is stored.
    request.session["new_token"] = raw
    messages.success(request, "Token created. Copy it now — it cannot be shown again.")
    return redirect("crm:member_list")


@lead_required
@require_POST
def token_revoke(request, pk):
    token = get_object_or_404(ApiToken, pk=pk)
    token.revoke()
    messages.success(request, f"Revoked token {token.key_prefix}… for {token.member.name}.")
    return redirect("crm:member_list")
