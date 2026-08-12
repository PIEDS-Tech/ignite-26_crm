"""Who is using the CRM, and how they proved it.

Two doors, one per batch, because the two batches have different problems:

- **Batch 2024 (leads)** pick their name from a list. No password. Every lead
  runs the CRM on their own laptop against the shared database, so the only
  person who can reach that form is the person holding the machine. A password
  here would protect nothing that the laptop's own lock screen doesn't.

- **Batch 2025** sign in with Google, restricted to the BITS domain. They were
  going to authenticate with Google anyway -- the sending agent already refuses
  to run unless the Gmail session matches the member -- so this reuses an
  identity they must prove regardless, rather than inventing a second one.

Identity is a session key holding a TeamMember id. Django's `User` model is no
longer consulted for the CRM at all; it survives only for `/admin/`.
"""

import os

from django.conf import settings
from django.urls import reverse

from crm.models import TeamMember
from shared.enums import LEAD_BATCH

#: The session key. A TeamMember UUID as a string.
SESSION_KEY = "member_id"

#: Where Google is redirected back to, and where we stash the CSRF state.
STATE_SESSION_KEY = "google_oauth_state"

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]


class GoogleAuthError(RuntimeError):
    """Raised with a message that is safe to show the person signing in."""


# --- session ---------------------------------------------------------------


def login_member(request, member: TeamMember) -> None:
    # Cycling the session key on login means a session id captured before
    # sign-in cannot be replayed afterwards.
    request.session.cycle_key()
    request.session[SESSION_KEY] = str(member.id)


def logout_member(request) -> None:
    request.session.pop(SESSION_KEY, None)
    request.session.pop(STATE_SESSION_KEY, None)


def current_member(request) -> TeamMember | None:
    """Resolve the signed-in TeamMember, or None.

    Deactivating a member takes effect on their next request rather than at
    their next login, which is what "their agent stops working immediately" in
    the playbook has to mean for the browser too.
    """
    member_id = request.session.get(SESSION_KEY)
    if not member_id:
        return None

    member = TeamMember.objects.filter(pk=member_id, is_active=True).first()
    if member is None:
        request.session.pop(SESSION_KEY, None)
    return member


def name_login_allowed(member: TeamMember) -> bool:
    """Only leads may sign in by name.

    Batch 2025 goes through Google because `sent_by` attribution follows the
    contacts assigned to them; letting anyone claim that identity from a
    dropdown would make the audit trail a suggestion.
    """
    return bool(member and member.is_active and member.batch == LEAD_BATCH)


def name_login_choices():
    return TeamMember.objects.filter(batch=LEAD_BATCH, is_active=True).order_by("name")


# --- google ----------------------------------------------------------------


def google_enabled() -> bool:
    return bool(settings.GOOGLE_OAUTH_CLIENT_ID and settings.GOOGLE_OAUTH_CLIENT_SECRET)


def _flow(request):
    from google_auth_oauthlib.flow import Flow

    # Redirect URIs are http://localhost:8000/... in this deployment shape --
    # every member runs the CRM on their own machine. oauthlib rejects plain
    # http by default, and Google returns scopes in its own order.
    if settings.DEBUG:
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

    return Flow.from_client_config(
        {
            "web": {
                "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            }
        },
        scopes=SCOPES,
        redirect_uri=request.build_absolute_uri(reverse("google_callback")),
    )


def google_authorization_url(request) -> str:
    flow = _flow(request)
    url, state = flow.authorization_url(
        access_type="online",
        include_granted_scopes="true",
        prompt="select_account",
        # Asks Google to show only BITS accounts. It is a hint, not a control:
        # the real check is on the verified `hd` claim below.
        hd=settings.GOOGLE_OAUTH_HOSTED_DOMAIN or None,
    )
    request.session[STATE_SESSION_KEY] = state
    return url


def member_from_google_callback(request) -> TeamMember:
    """Exchange the code, verify the token, and map it to a TeamMember.

    Every failure here is a refusal to sign anyone in. There is no path that
    creates a member -- a person who is not already in the pool has no business
    holding a session, and adding them is a lead's decision.
    """
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    expected_state = request.session.pop(STATE_SESSION_KEY, None)
    if not expected_state or request.GET.get("state") != expected_state:
        raise GoogleAuthError("Sign-in state did not match. Please try again.")

    if request.GET.get("error"):
        raise GoogleAuthError("Google sign-in was cancelled.")

    flow = _flow(request)
    try:
        flow.fetch_token(code=request.GET.get("code"))
    except Exception as exc:                                   # noqa: BLE001
        raise GoogleAuthError(f"Could not complete Google sign-in: {exc}")

    # The id_token is signed by Google and carries the email; verifying it is
    # what makes this trustworthy rather than the access token, which only says
    # a call succeeded.
    claims = google_id_token.verify_oauth2_token(
        flow.credentials.id_token,
        google_requests.Request(),
        settings.GOOGLE_OAUTH_CLIENT_ID,
    )

    if not claims.get("email_verified"):
        raise GoogleAuthError("That Google account has no verified email address.")

    domain = settings.GOOGLE_OAUTH_HOSTED_DOMAIN
    if domain and claims.get("hd") != domain:
        raise GoogleAuthError(f"Sign in with your @{domain} account, not a personal one.")

    email = claims["email"].lower()
    member = TeamMember.objects.filter(bits_email__iexact=email, is_active=True).first()
    if member is None:
        raise GoogleAuthError(
            f"{email} is not an active team member. Ask a lead to add you first."
        )
    return member
