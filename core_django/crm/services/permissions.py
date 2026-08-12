"""The one place that defines what a "lead" is.

The permission rule is "batch 2024 members may assign contacts". That literal
lives in shared/enums.py::LEAD_BATCH and is read only from here, so changing
which batch leads next year is a one-line edit.
"""

from functools import wraps

from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect

from shared.enums import LEAD_BATCH


def get_member(request):
    """Resolve the signed-in TeamMember, or None.

    Identity is a session key, not a Django `User` -- see services/auth.py.
    """
    from .auth import current_member

    return current_member(request)


def is_lead(member) -> bool:
    return bool(member and member.is_active and member.batch == LEAD_BATCH)


def lead_required(view_func):
    """Restrict a view to active lead-batch members."""

    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        member = get_member(request)
        # Nobody signed in is a wrong turn -- send them to the front door.
        # Signed in but not a lead is a real refusal, and says so.
        if member is None:
            return redirect("login")
        if not is_lead(member):
            raise PermissionDenied(
                f"Only active batch-{LEAD_BATCH} members may access this page."
            )
        request.member = member
        return view_func(request, *args, **kwargs)

    return _wrapped


def member_required(view_func):
    """Restrict a view to any active team member."""

    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        member = get_member(request)
        if member is None:
            return redirect("login")
        request.member = member
        return view_func(request, *args, **kwargs)

    return _wrapped


# --- contact-level rules --------------------------------------------------
# Leads own the whole pool. Everyone else owns exactly what is assigned to
# them: they are the person actually in conversation with that prospect, so
# they are the one who knows when a designation is wrong -- but a stale row
# in someone else's list is not theirs to touch.


def can_edit_contact(member, contact) -> bool:
    """Whether `member` may change this contact's data at all."""
    if not member or not member.is_active:
        return False
    return is_lead(member) or contact.assigned_to_id == member.id


def can_set_lifecycle(member) -> bool:
    """Lifecycle is funnel truth the whole team reads. Leads only.

    Non-leads still move it implicitly by sending -- that path is in
    services/mailing.py and is the only automatic transition.
    """
    return is_lead(member)


def can_hard_delete(member, contact) -> bool:
    """Permanent deletion: leads only, and only for a contact never mailed.

    CampaignMailing.contact is on_delete=PROTECT, so a mailed contact cannot be
    removed from the table at all. Checking here turns an IntegrityError 500
    into a sentence the user can act on. Archiving is the answer for the rest.
    """
    return is_lead(member) and not contact.mailings.exists()


def editable_contacts(member):
    """The queryset `member` is permitted to mutate.

    Bulk operations filter through this rather than trusting posted IDs, so a
    member submitting a hand-crafted list silently affects nothing outside it.
    """
    from crm.models import Contact

    qs = Contact.objects.all()
    return qs if is_lead(member) else qs.filter(assigned_to=member)
