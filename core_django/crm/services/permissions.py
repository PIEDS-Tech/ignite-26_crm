"""The one place that defines what a "lead" is.

The permission rule is "batch 2024 members may assign contacts". That literal
lives in shared/enums.py::LEAD_BATCH and is read only from here, so changing
which batch leads next year is a one-line edit.
"""

from functools import wraps

from django.core.exceptions import PermissionDenied

from shared.enums import LEAD_BATCH


def get_member(user):
    """Resolve the TeamMember behind a Django login, or None."""
    return getattr(user, "team_member", None)


def is_lead(member) -> bool:
    return bool(member and member.is_active and member.batch == LEAD_BATCH)


def lead_required(view_func):
    """Restrict a view to active lead-batch members."""

    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        member = get_member(request.user) if request.user.is_authenticated else None
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
        member = get_member(request.user) if request.user.is_authenticated else None
        if not member or not member.is_active:
            raise PermissionDenied("You are not registered as an active team member.")
        request.member = member
        return view_func(request, *args, **kwargs)

    return _wrapped
