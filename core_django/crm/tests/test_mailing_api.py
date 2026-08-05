"""The claim/report protocol, and the guarantees that moved server-side.

The most important test in this file is
`test_claiming_twice_yields_no_second_mailing` — if that ever fails, the system
can put two copies of the same mail in a prospect's inbox.
"""

import json

import pytest
from django.urls import reverse

from crm.models import ApiToken, Campaign, CampaignMailing, Contact, TeamMember
from crm.services import mailing as mailing_svc
from shared.enums import CampaignStatus, MailingStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def member():
    return TeamMember.objects.create(
        name="Kabir", bits_email="kabir@pilani.bits-pilani.ac.in", batch="2025"
    )


@pytest.fixture
def other_member():
    return TeamMember.objects.create(
        name="Ishita", bits_email="ishita@pilani.bits-pilani.ac.in", batch="2025"
    )


@pytest.fixture
def token(member):
    _, raw = ApiToken.issue(member, "test laptop")
    return raw


@pytest.fixture
def auth(token):
    return {"HTTP_AUTHORIZATION": f"Token {token}"}


@pytest.fixture
def campaign(member):
    return Campaign.objects.create(
        title="Outreach",
        mail_sub="{{ company }} x PIEDS",
        mail_body="Hi {{ first_name }}, about your work as {{ designation }}.",
        var_list=["company", "first_name", "designation"],
        status=CampaignStatus.ACTIVE.value,
        created_by=member,
    )


@pytest.fixture
def contact(member):
    return Contact.objects.create(
        first_name="Rohan", last_name="Iyer", email="rohan@example.com",
        company="Zerodha", designation="CTO", assigned_to=member,
    )


# ------------------------------------------------------------------- auth

class TestTokenAuth:
    def test_missing_header_is_rejected(self, client):
        assert client.get(reverse("api:me")).status_code == 401

    def test_bad_token_is_rejected(self, client):
        r = client.get(reverse("api:me"), HTTP_AUTHORIZATION="Token nonsense")
        assert r.status_code == 401

    def test_valid_token_identifies_the_member(self, client, auth, member):
        r = client.get(reverse("api:me"), **auth)
        assert r.status_code == 200
        assert r.json()["bits_email"] == member.bits_email

    def test_revoked_token_is_rejected(self, client, auth, member):
        member.api_tokens.first().revoke()
        assert client.get(reverse("api:me"), **auth).status_code == 401

    def test_only_the_hash_is_stored(self, member, token):
        stored = member.api_tokens.first()
        assert token not in stored.key_hash
        assert stored.key_hash == ApiToken.hash_key(token)

    def test_last_used_is_stamped(self, client, auth, member):
        assert member.api_tokens.first().last_used_at is None
        client.get(reverse("api:me"), **auth)
        assert member.api_tokens.first().last_used_at is not None


# ------------------------------------------------------------------ claim

def claim(client, auth, campaign, contact_ids):
    return client.post(
        reverse("api:claim"),
        data=json.dumps({"campaign_id": str(campaign.id),
                         "contact_ids": [str(c) for c in contact_ids]}),
        content_type="application/json",
        **auth,
    )


class TestClaim:
    def test_claim_creates_a_draft_with_rendered_content(
        self, client, auth, campaign, contact
    ):
        body = claim(client, auth, campaign, [contact.id]).json()

        assert len(body["claimed"]) == 1
        item = body["claimed"][0]
        assert item["to"] == contact.email
        assert item["subject"] == "Zerodha x PIEDS"
        assert "Hi Rohan" in item["body"] and "as CTO" in item["body"]

        mailing = CampaignMailing.objects.get(id=item["mailing_id"])
        assert mailing.status == MailingStatus.DRAFT.value
        # The snapshot must outlive later edits to the campaign template.
        assert mailing.rendered_subject == "Zerodha x PIEDS"

    def test_claiming_twice_yields_no_second_mailing(
        self, client, auth, campaign, contact
    ):
        """The guarantee the whole product rests on."""
        first = claim(client, auth, campaign, [contact.id]).json()
        second = claim(client, auth, campaign, [contact.id]).json()

        assert len(first["claimed"]) == 1
        assert second["claimed"] == []
        assert "already has a mailing" in second["skipped"][0]["reason"]
        assert CampaignMailing.objects.filter(contact=contact).count() == 1

    def test_contact_assigned_to_someone_else_is_refused(
        self, client, auth, campaign, contact, other_member
    ):
        contact.assigned_to = other_member
        contact.save()

        body = claim(client, auth, campaign, [contact.id]).json()

        assert body["claimed"] == []
        assert "not assigned" in body["skipped"][0]["reason"]
        assert not CampaignMailing.objects.exists()

    def test_blank_required_variable_blocks_the_claim(
        self, client, auth, campaign, contact
    ):
        contact.designation = ""
        contact.save()

        body = claim(client, auth, campaign, [contact.id]).json()

        assert body["claimed"] == []
        assert "designation" in body["skipped"][0]["reason"]
        # No row written, so the contact stays sendable once data is fixed.
        assert not CampaignMailing.objects.exists()

    def test_non_active_campaign_is_refused(self, client, auth, campaign, contact):
        campaign.status = CampaignStatus.DRAFT.value
        campaign.save()

        r = claim(client, auth, campaign, [contact.id])
        assert r.status_code == 400
        assert "draft" in r.json()["error"]


# ----------------------------------------------------------------- report

class TestReportResult:
    def _claim_one(self, client, auth, campaign, contact):
        return claim(client, auth, campaign, [contact.id]).json()["claimed"][0]

    def post_result(self, client, auth, mailing_id, payload):
        return client.post(
            reverse("api:result", args=[mailing_id]),
            data=json.dumps(payload), content_type="application/json", **auth,
        )

    def test_sent_updates_mailing_and_contact(self, client, auth, campaign, contact):
        item = self._claim_one(client, auth, campaign, contact)

        r = self.post_result(client, auth, item["mailing_id"], {
            "status": "sent", "message_id": "msg-1", "thread_id": "thr-1",
        })
        assert r.json()["status"] == "SENT"

        mailing = CampaignMailing.objects.get(id=item["mailing_id"])
        assert mailing.status == MailingStatus.SENT.value
        assert mailing.mail_thread_id == "thr-1"

        contact.refresh_from_db()
        assert contact.last_contacted_at is not None

    def test_failed_records_the_error(self, client, auth, campaign, contact):
        item = self._claim_one(client, auth, campaign, contact)

        self.post_result(client, auth, item["mailing_id"],
                         {"status": "failed", "error": "quota exceeded"})

        mailing = CampaignMailing.objects.get(id=item["mailing_id"])
        assert mailing.status == MailingStatus.FAILED.value
        assert "quota exceeded" in mailing.error_detail

    def test_replayed_report_does_not_overwrite(self, client, auth, campaign, contact):
        """A retried HTTP request must not turn a SENT mailing into FAILED."""
        item = self._claim_one(client, auth, campaign, contact)
        self.post_result(client, auth, item["mailing_id"],
                         {"status": "sent", "thread_id": "thr-1"})

        r = self.post_result(client, auth, item["mailing_id"],
                             {"status": "failed", "error": "late duplicate"})
        assert r.json()["detail"] == "already settled"

        mailing = CampaignMailing.objects.get(id=item["mailing_id"])
        assert mailing.status == MailingStatus.SENT.value

    def test_cannot_report_another_members_mailing(
        self, client, auth, campaign, contact, other_member
    ):
        item = self._claim_one(client, auth, campaign, contact)
        _, other_raw = ApiToken.issue(other_member)

        r = client.post(
            reverse("api:result", args=[item["mailing_id"]]),
            data=json.dumps({"status": "sent"}), content_type="application/json",
            HTTP_AUTHORIZATION=f"Token {other_raw}",
        )
        assert r.json()["status"] == "NOT_ASSIGNED"

    def test_invalid_status_is_rejected(self, client, auth, campaign, contact):
        item = self._claim_one(client, auth, campaign, contact)
        r = self.post_result(client, auth, item["mailing_id"], {"status": "maybe"})
        assert r.status_code == 400


# -------------------------------------------------------- drafts & retry

class TestDraftsAndRetry:
    def test_stranded_draft_is_listed(self, client, auth, campaign, contact):
        claim(client, auth, campaign, [contact.id])

        body = client.get(reverse("api:drafts"), **auth).json()
        assert len(body) == 1
        assert body[0]["to"] == contact.email
        assert body[0]["subject"] == "Zerodha x PIEDS"

    def test_retry_reuses_the_row_rather_than_inserting(
        self, client, auth, campaign, contact, member
    ):
        item = claim(client, auth, campaign, [contact.id]).json()["claimed"][0]
        mailing_svc.record_result(item["mailing_id"], member,
                                  status="failed", error="boom")

        assert mailing_svc.reset_for_retry(item["mailing_id"], member) is True

        mailing = CampaignMailing.objects.get(id=item["mailing_id"])
        assert mailing.status == MailingStatus.DRAFT.value
        assert mailing.error_detail == ""
        assert CampaignMailing.objects.filter(contact=contact).count() == 1


# -------------------------------------------------------------- preflight

class TestPreflight:
    def test_reports_each_contact_without_writing_anything(
        self, client, auth, campaign, contact, member
    ):
        blank = Contact.objects.create(
            first_name="Ana", email="ana@example.com", company="Postman",
            designation="", assigned_to=member,
        )

        r = client.post(
            reverse("api:preflight"),
            data=json.dumps({"campaign_id": str(campaign.id),
                             "contact_ids": [str(contact.id), str(blank.id)]}),
            content_type="application/json", **auth,
        )
        body = r.json()

        states = {o["contact_id"]: o["status"] for o in body["outcomes"]}
        assert states[str(contact.id)] == "OK"
        assert states[str(blank.id)] == "MISSING_VARS"
        assert body["sendable"] == 1
        assert body["preview"]["subject"] == "Zerodha x PIEDS"

        # A dry run must not create rows.
        assert not CampaignMailing.objects.exists()
