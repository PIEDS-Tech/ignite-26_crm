"""The front door. Kept out of views.py because everything there is already
past the gate -- these four views are the only ones a stranger can reach.
"""

from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from shared.enums import LEAD_BATCH

from .models import TeamMember
from .services import auth as auth_svc


def login_page(request):
    if auth_svc.current_member(request):
        return redirect("crm:home")

    return render(request, "crm/login.html", {
        "leads": auth_svc.name_login_choices(),
        "google_enabled": auth_svc.google_enabled(),
        "lead_batch": LEAD_BATCH,
    })


@require_POST
def login_by_name(request):
    """Batch 2024 only. See services/auth.py for why that asymmetry exists."""
    member = TeamMember.objects.filter(pk=request.POST.get("member_id")).first()

    if not member or not auth_svc.name_login_allowed(member):
        messages.error(request, "Pick your name from the list to continue.")
        return redirect("login")

    auth_svc.login_member(request, member)
    return redirect("crm:home")


def google_login(request):
    if not auth_svc.google_enabled():
        messages.error(
            request,
            "Google sign-in is not configured on this machine. Set "
            "GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET in .env.",
        )
        return redirect("login")

    return redirect(auth_svc.google_authorization_url(request))


def google_callback(request):
    try:
        member = auth_svc.member_from_google_callback(request)
    except auth_svc.GoogleAuthError as exc:
        messages.error(request, str(exc))
        return redirect("login")

    auth_svc.login_member(request, member)
    return redirect("crm:home")


@require_POST
def logout_view(request):
    auth_svc.logout_member(request)
    return redirect("login")
