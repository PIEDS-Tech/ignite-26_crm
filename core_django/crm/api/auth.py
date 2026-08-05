"""Token authentication for the local sending agents.

Deliberately hand-rolled rather than pulling in DRF: seven endpoints and one
header do not justify the dependency, and the logic here is short enough to
audit in one sitting.
"""

import json
from functools import wraps

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from crm.models import ApiToken


def json_error(message, status=400):
    return JsonResponse({"error": message}, status=status)


def api_token_required(view_func):
    """Resolve `Authorization: Token …` to a TeamMember.

    CSRF is exempted because these endpoints are called by a server-side HTTP
    client with a bearer token, not by a browser carrying a session cookie --
    there is no ambient authority for an attacker to ride.
    """

    @csrf_exempt
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        header = request.headers.get("Authorization", "")
        scheme, _, raw = header.partition(" ")
        if scheme.lower() != "token" or not raw.strip():
            return json_error("Expected 'Authorization: Token <key>'.", 401)

        token = (
            ApiToken.objects.select_related("member")
            .filter(key_hash=ApiToken.hash_key(raw.strip()))
            .first()
        )
        if token is None:
            return json_error("Unknown token.", 401)
        if token.revoked_at is not None:
            return json_error("This token has been revoked.", 401)
        if not token.member.is_active:
            return json_error("This team member is not active.", 403)

        # Cheap last-seen tracking so leads can spot stale or leaked tokens.
        ApiToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())

        request.member = token.member
        request.api_token = token
        return view_func(request, *args, **kwargs)

    return _wrapped


def parse_json(request):
    """Return (payload, error_response)."""
    try:
        return json.loads(request.body or b"{}"), None
    except json.JSONDecodeError:
        return None, json_error("Request body must be valid JSON.")
