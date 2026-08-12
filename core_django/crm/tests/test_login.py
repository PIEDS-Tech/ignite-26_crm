"""The front door.

The load-bearing rule here is that batch 2025 cannot sign in by name. The name
list is a convenience for leads on their own machine; if a 2025 member could
pick a lead out of it, every permission in the system would be advisory.
"""

import pytest
from django.urls import reverse

from crm.models import TeamMember
from crm.services import auth as auth_svc

pytestmark = pytest.mark.django_db


@pytest.fixture
def lead():
    return TeamMember.objects.create(
        name="Aarav", bits_email="aarav@pilani.bits-pilani.ac.in", batch="2024"
    )


@pytest.fixture
def member():
    return TeamMember.objects.create(
        name="Kabir", bits_email="kabir@pilani.bits-pilani.ac.in", batch="2025"
    )


class TestNameLogin:
    def test_only_leads_are_offered(self, client, lead, member):
        body = client.get(reverse("login")).content.decode()
        assert "Aarav" in body
        assert "Kabir" not in body

    def test_a_lead_signs_in_by_name(self, client, lead):
        r = client.post(reverse("login_by_name"), {"member_id": str(lead.id)})
        assert r.status_code == 302
        assert client.session[auth_svc.SESSION_KEY] == str(lead.id)

    def test_a_2025_member_cannot_sign_in_by_name(self, client, member):
        """Not merely hidden from the list -- refused when posted directly."""
        client.post(reverse("login_by_name"), {"member_id": str(member.id)})
        assert auth_svc.SESSION_KEY not in client.session

    def test_an_inactive_lead_cannot_sign_in(self, client, lead):
        TeamMember.objects.filter(pk=lead.pk).update(is_active=False)
        client.post(reverse("login_by_name"), {"member_id": str(lead.id)})
        assert auth_svc.SESSION_KEY not in client.session

    def test_a_bogus_id_signs_nobody_in(self, client, lead):
        client.post(reverse("login_by_name"),
                    {"member_id": "11111111-1111-1111-1111-111111111111"})
        assert auth_svc.SESSION_KEY not in client.session


class TestSession:
    def test_a_stranger_is_sent_to_the_login_page(self, client):
        r = client.get(reverse("crm:home"))
        assert r.status_code == 302
        assert r["Location"] == reverse("login")

    def test_logout_clears_the_session(self, client, lead):
        client.post(reverse("login_by_name"), {"member_id": str(lead.id)})
        client.post(reverse("logout"))
        assert auth_svc.SESSION_KEY not in client.session

    def test_deactivating_a_member_ends_their_session(self, client, lead):
        """Takes effect on the next request, not at their next login."""
        client.post(reverse("login_by_name"), {"member_id": str(lead.id)})
        TeamMember.objects.filter(pk=lead.pk).update(is_active=False)

        assert client.get(reverse("crm:home")).status_code == 302


class TestGoogleDoor:
    def test_it_is_offered_when_configured(self, client, settings, lead):
        settings.GOOGLE_OAUTH_CLIENT_ID = "id"
        settings.GOOGLE_OAUTH_CLIENT_SECRET = "secret"
        assert "Sign in with your BITS Google account" in (
            client.get(reverse("login")).content.decode()
        )

    def test_unconfigured_says_so_instead_of_erroring(self, client, settings, lead):
        settings.GOOGLE_OAUTH_CLIENT_ID = ""
        settings.GOOGLE_OAUTH_CLIENT_SECRET = ""

        assert "Not configured on this machine" in (
            client.get(reverse("login")).content.decode()
        )
        # And the route itself redirects rather than raising.
        assert client.get(reverse("google_login")).status_code == 302

    def test_a_forged_callback_signs_nobody_in(self, client, settings, member):
        """No state in the session means the response did not come from a flow
        we started."""
        settings.GOOGLE_OAUTH_CLIENT_ID = "id"
        settings.GOOGLE_OAUTH_CLIENT_SECRET = "secret"

        r = client.get(reverse("google_callback"), {"state": "made-up", "code": "x"})
        assert r.status_code == 302
        assert auth_svc.SESSION_KEY not in client.session
