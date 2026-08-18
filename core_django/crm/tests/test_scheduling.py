"""Scheduled sends: the queue, the lease, and the rules around them.

The load-bearing tests here are `test_a_leased_job_is_invisible_to_a_second_agent`
and `test_a_job_never_crosses_to_another_member`. The first is what stops two
agents racing through the same batch; the second is what stops a mail leaving
the wrong mailbox with a false sent_by.

Nothing here sleeps. Every timing rule takes `now` as a parameter precisely so
a six-hour grace window is a six-line test.
"""

import json
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from crm.models import ApiToken, Campaign, CampaignMailing, Contact, ScheduledSend, TeamMember
from crm.services import scheduling as svc
from shared.enums import CampaignStatus, ContactLifecycle, MailingStatus, ScheduleStatus

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
def auth(member):
    _, raw = ApiToken.issue(member, "test laptop")
    return {"HTTP_AUTHORIZATION": f"Token {raw}"}


@pytest.fixture
def campaign(member):
    return Campaign.objects.create(
        title="Outreach",
        mail_sub="Hello {{ first_name }}",
        mail_body="Hi {{ first_name }}.",
        var_list=["first_name"],
        status=CampaignStatus.ACTIVE.value,
        created_by=member,
    )


def make_contact(member, n=0, **kwargs):
    fields = dict(
        first_name=f"Rohan{n}", last_name="Iyer", email=f"rohan{n}@example.com",
        company="Acme", designation="CTO", assigned_to=member, created_by=member,
    )
    fields.update(kwargs)
    return Contact.objects.create(**fields)


@pytest.fixture
def contact(member):
    return make_contact(member)


def schedule(campaign, member, contacts, *, when=None, **kwargs):
    """Queue a job, optionally one that is already due.

    `create` refuses a time in the past on purpose, so a due job is made by
    creating a future one and then moving the clock hand back on the row --
    which is what actually happens in production anyway. Loosening the
    validation for the convenience of the tests would test different code from
    the one that runs.
    """
    job = svc.create(
        campaign_id=campaign.id,
        member=member,
        contact_ids=[c.id for c in contacts],
        scheduled_at=timezone.now() + timedelta(hours=1),
        **kwargs,
    )
    if when is not None:
        ScheduledSend.objects.filter(id=job.id).update(scheduled_at=when)
        job.refresh_from_db()
    return job


def due_now():
    """A moment safely inside the past, beyond the clock-skew tolerance."""
    return timezone.now() - timedelta(minutes=5)


# ------------------------------------------------------------------ creating

def test_create_stores_the_selection_and_starts_pending(campaign, member, contact):
    job = schedule(campaign, member, [contact])

    assert job.status == ScheduleStatus.PENDING.value
    assert job.contact_ids == [str(contact.id)]
    assert job.cursor == 0 and job.total == 1 and job.remaining == 1


def test_a_time_in_the_past_is_refused(campaign, member, contact):
    with pytest.raises(svc.NotSchedulable):
        svc.create(
            campaign_id=campaign.id, member=member, contact_ids=[contact.id],
            scheduled_at=timezone.now() - timedelta(hours=2),
        )


def test_an_empty_selection_is_refused(campaign, member):
    with pytest.raises(svc.NotSchedulable):
        svc.create(
            campaign_id=campaign.id, member=member, contact_ids=[],
            scheduled_at=timezone.now() + timedelta(hours=1),
        )


def test_a_non_active_campaign_cannot_be_scheduled(campaign, member, contact):
    campaign.status = CampaignStatus.PAUSED.value
    campaign.save()
    with pytest.raises(svc.NotSchedulable):
        svc.create(
            campaign_id=campaign.id, member=member, contact_ids=[contact.id],
            scheduled_at=timezone.now() + timedelta(hours=1),
        )


def test_a_bad_cc_is_refused_at_creation(campaign, member, contact):
    """Same validator as the manual send path -- one definition of a valid CC."""
    with pytest.raises(svc.NotSchedulable):
        svc.create(
            campaign_id=campaign.id, member=member, contact_ids=[contact.id],
            scheduled_at=timezone.now() + timedelta(hours=1), cc="not-an-email",
        )


def test_duplicate_contacts_are_collapsed_but_order_is_kept(campaign, member):
    a, b = make_contact(member, 1), make_contact(member, 2)
    job = svc.create(
        campaign_id=campaign.id, member=member,
        contact_ids=[a.id, b.id, a.id],
        scheduled_at=timezone.now() + timedelta(hours=1),
    )
    assert job.contact_ids == [str(a.id), str(b.id)]


# ------------------------------------------------------------------ claiming

def test_a_job_is_not_claimable_before_it_is_due(campaign, member, contact):
    schedule(campaign, member, [contact], when=timezone.now() + timedelta(hours=3))
    assert svc.claim_due(member) == []


def test_a_due_job_is_claimed_and_leased(campaign, member, contact):
    job = schedule(campaign, member, [contact], when=due_now())

    claimed = svc.claim_due(member, agent_id="always-on")
    assert [j.id for j in claimed] == [job.id]

    job.refresh_from_db()
    assert job.status == ScheduleStatus.RUNNING.value
    assert job.leased_by == "always-on"
    assert job.lease_expires_at > timezone.now()
    assert job.attempts == 1 and job.started_at is not None


def test_a_leased_job_is_invisible_to_a_second_agent(campaign, member, contact):
    """Two of this member's agents polling at once must not both run the batch."""
    schedule(campaign, member, [contact], when=due_now())

    assert len(svc.claim_due(member, agent_id="laptop")) == 1
    assert svc.claim_due(member, agent_id="always-on") == []


def test_a_job_never_crosses_to_another_member(campaign, member, other_member, contact):
    """An agent authenticates as one member; sending someone else's job would
    leave the wrong mailbox and record a false sent_by."""
    schedule(campaign, member, [contact], when=due_now())
    assert svc.claim_due(other_member) == []


def test_a_paused_campaign_holds_the_job_rather_than_sending_it(campaign, member, contact):
    job = schedule(campaign, member, [contact], when=due_now())
    campaign.status = CampaignStatus.PAUSED.value
    campaign.save()

    assert svc.claim_due(member) == []
    job.refresh_from_db()
    assert job.status == ScheduleStatus.HELD.value
    assert "paused" in job.last_error


def test_a_held_job_resumes_once_the_campaign_is_active_again(campaign, member, contact):
    job = schedule(campaign, member, [contact], when=due_now())
    campaign.status = CampaignStatus.PAUSED.value
    campaign.save()
    svc.claim_due(member)

    campaign.status = CampaignStatus.ACTIVE.value
    campaign.save()
    assert len(svc.claim_due(member)) == 1
    job.refresh_from_db()
    assert job.status == ScheduleStatus.RUNNING.value


# -------------------------------------------------------------------- leases

def test_an_expired_lease_is_swept_back_to_pending(campaign, member, contact):
    """A laptop that closed its lid mid-batch must not strand the job."""
    job = schedule(campaign, member, [contact], when=due_now())
    svc.claim_due(member)

    ScheduledSend.objects.filter(id=job.id).update(
        lease_expires_at=timezone.now() - timedelta(minutes=1)
    )
    assert svc.sweep_expired_leases() == 1

    job.refresh_from_db()
    assert job.status == ScheduleStatus.PENDING.value
    assert job.leased_by == ""
    assert len(svc.claim_due(member)) == 1


def test_a_live_lease_is_left_alone(campaign, member, contact):
    schedule(campaign, member, [contact], when=due_now())
    svc.claim_due(member)
    assert svc.sweep_expired_leases() == 0


def test_heartbeat_extends_a_lease(campaign, member, contact):
    job = schedule(campaign, member, [contact], when=due_now())
    svc.claim_due(member)
    job.refresh_from_db()
    before = job.lease_expires_at

    svc.heartbeat(job.id, member, now=timezone.now() + timedelta(minutes=2))
    job.refresh_from_db()
    assert job.lease_expires_at > before


# ------------------------------------------------------------------ progress

def test_progress_advances_the_cursor_and_finishes_the_job(campaign, member):
    contacts = [make_contact(member, i) for i in range(3)]
    job = schedule(campaign, member, contacts, when=due_now())
    svc.claim_due(member)

    out = svc.record_progress(job.id, member, attempted=3, sent=2, skipped=1)
    assert out["status"] == ScheduleStatus.DONE.value

    job.refresh_from_db()
    assert (job.cursor, job.sent_count, job.skipped_count) == (3, 2, 1)
    assert job.finished_at is not None and job.remaining == 0


def test_the_cursor_advances_past_permanently_skipped_contacts(campaign, member):
    """A do_not_contact contact never gets a mailing row. If progress were
    measured by "contacts still lacking a mailing", the job would never end."""
    good = make_contact(member, 1)
    blocked = make_contact(member, 2, lifecycle=ContactLifecycle.DO_NOT_CONTACT.value)
    job = schedule(campaign, member, [good, blocked], when=due_now())
    svc.claim_due(member)

    svc.record_progress(job.id, member, attempted=2, sent=1, skipped=1)
    job.refresh_from_db()

    assert job.status == ScheduleStatus.DONE.value
    assert CampaignMailing.objects.filter(contact=blocked).count() == 0


def test_a_partial_slice_returns_the_job_to_the_queue(campaign, member):
    contacts = [make_contact(member, i) for i in range(5)]
    job = schedule(campaign, member, contacts, when=due_now())
    svc.claim_due(member)

    svc.record_progress(job.id, member, attempted=2, sent=2, skipped=0)
    job.refresh_from_db()

    assert job.status == ScheduleStatus.PENDING.value
    assert job.cursor == 2 and job.remaining == 3
    assert job.next_slice() == [str(c.id) for c in contacts[2:]]


def test_progress_from_another_member_is_refused(campaign, member, other_member, contact):
    job = schedule(campaign, member, [contact], when=due_now())
    out = svc.record_progress(job.id, other_member, attempted=1, sent=1, skipped=0)
    assert out["status"] == "unknown"


def test_mark_failed_is_terminal(campaign, member, contact):
    job = schedule(campaign, member, [contact], when=due_now())
    svc.mark_failed(job.id, member, "gmail auth died")

    job.refresh_from_db()
    assert job.status == ScheduleStatus.FAILED.value
    assert job.is_terminal and "gmail" in job.last_error
    assert svc.claim_due(member) == []


# ------------------------------------------------------------- human control

def test_cancel_stops_a_pending_job(campaign, member, contact):
    job = schedule(campaign, member, [contact])
    svc.cancel(job.id, member=member)

    job.refresh_from_db()
    assert job.status == ScheduleStatus.CANCELLED.value
    assert svc.claim_due(member) == []


def test_cancelling_mid_flight_stops_the_next_slice(campaign, member):
    """Mail already sent stays sent; the job simply does not continue."""
    contacts = [make_contact(member, i) for i in range(4)]
    job = schedule(campaign, member, contacts, when=due_now())
    svc.claim_due(member)
    svc.cancel(job.id, member=member)

    out = svc.record_progress(job.id, member, attempted=2, sent=2, skipped=0)
    assert out["status"] == ScheduleStatus.CANCELLED.value

    job.refresh_from_db()
    assert job.status == ScheduleStatus.CANCELLED.value


def test_a_terminal_job_cannot_be_cancelled_twice(campaign, member, contact):
    job = schedule(campaign, member, [contact])
    svc.cancel(job.id, member=member)
    with pytest.raises(svc.NotSchedulable):
        svc.cancel(job.id, member=member)


def test_reschedule_moves_the_time_and_clears_a_hold(campaign, member, contact):
    job = schedule(campaign, member, [contact], when=due_now())
    campaign.status = CampaignStatus.PAUSED.value
    campaign.save()
    svc.claim_due(member)
    job.refresh_from_db()
    assert job.status == ScheduleStatus.HELD.value

    later = timezone.now() + timedelta(days=1)
    campaign.status = CampaignStatus.ACTIVE.value
    campaign.save()
    svc.reschedule(job.id, later, member=member)

    job.refresh_from_db()
    assert job.status == ScheduleStatus.PENDING.value
    assert abs((job.scheduled_at - later).total_seconds()) < 1
    assert job.last_error == ""


def test_a_running_job_must_be_cancelled_not_rescheduled(campaign, member, contact):
    job = schedule(campaign, member, [contact], when=due_now())
    svc.claim_due(member)
    with pytest.raises(svc.NotSchedulable):
        svc.reschedule(job.id, timezone.now() + timedelta(days=1), member=member)


# ----------------------------------------------------------------------- API

def _post(client, url, payload, auth):
    return client.post(url, data=json.dumps(payload), content_type="application/json", **auth)


def test_api_creates_and_lists_a_schedule(client, auth, campaign, contact, member):
    when = (timezone.now() + timedelta(hours=2)).isoformat()
    r = _post(client, reverse("api:schedules"),
              {"campaign_id": str(campaign.id), "contact_ids": [str(contact.id)],
               "scheduled_at": when, "cc": "lead@x.com"}, auth)
    assert r.status_code == 201, r.content
    assert r.json()["cc"] == "lead@x.com"

    listed = client.get(reverse("api:schedules"), **auth).json()
    assert len(listed) == 1 and listed[0]["total"] == 1


def test_api_rejects_a_malformed_time(client, auth, campaign, contact):
    r = _post(client, reverse("api:schedules"),
              {"campaign_id": str(campaign.id), "contact_ids": [str(contact.id)],
               "scheduled_at": "next tuesday"}, auth)
    assert r.status_code == 400
    assert "ISO 8601" in r.json()["error"]


def test_api_claim_returns_the_slice_to_send(client, auth, campaign, member):
    contacts = [make_contact(member, i) for i in range(3)]
    schedule(campaign, member, contacts, when=due_now())

    body = _post(client, reverse("api:schedule_claim"), {"agent_id": "docker"}, auth).json()
    assert len(body["claimed"]) == 1
    assert body["claimed"][0]["contact_ids"] == [str(c.id) for c in contacts]


def test_api_progress_and_cancel_round_trip(client, auth, campaign, member, contact):
    job = schedule(campaign, member, [contact], when=due_now())
    _post(client, reverse("api:schedule_claim"), {}, auth)

    out = _post(client, reverse("api:schedule_progress", args=[job.id]),
                {"attempted": 1, "sent": 1, "skipped": 0}, auth).json()
    assert out["status"] == ScheduleStatus.DONE.value

    r = _post(client, reverse("api:schedule_cancel", args=[job.id]), {}, auth)
    assert r.status_code == 400   # already done; nothing left to call off


# ------------------------------------------------------- the CRM schedule page

@pytest.fixture
def lead():
    return TeamMember.objects.create(
        name="Aarav", bits_email="aarav@pilani.bits-pilani.ac.in", batch="2024"
    )


def sign_in(client, who):
    session = client.session
    session["member_id"] = str(who.id)
    session.save()
    return client


def test_the_page_lists_open_jobs_and_hides_finished_ones(client, lead, campaign, member, contact):
    """Default view is work still outstanding; finished jobs are on ?status=all.

    Asserted on the rendered status classes rather than on primary keys: a
    terminal job renders no cancel form, so its pk never reaches the HTML.
    """
    schedule(campaign, member, [contact])
    done = schedule(campaign, member, [make_contact(member, 9)])
    ScheduledSend.objects.filter(id=done.id).update(status=ScheduleStatus.DONE.value)

    body = sign_in(client, lead).get(reverse("crm:schedule_list")).content.decode()
    assert "s-PENDING" in body
    assert "s-DONE" not in body

    all_body = client.get(reverse("crm:schedule_list"), {"status": "all"}).content.decode()
    assert "s-DONE" in all_body and "s-PENDING" in all_body


def test_missed_jobs_are_called_out_at_the_top(client, lead, campaign, member, contact):
    """The whole point of this page: mail that never went out must be loud."""
    job = schedule(campaign, member, [contact])
    ScheduledSend.objects.filter(id=job.id).update(
        status=ScheduleStatus.MISSED.value, last_error="nothing executed it"
    )

    body = sign_in(client, lead).get(reverse("crm:schedule_list")).content.decode()
    assert "need attention" in body
    assert "nothing executed it" in body


def test_a_lead_can_cancel_anyone_s_job(client, lead, campaign, member, contact):
    job = schedule(campaign, member, [contact])
    r = sign_in(client, lead).post(reverse("crm:schedule_cancel", args=[job.pk]))

    assert r.status_code == 302
    job.refresh_from_db()
    assert job.status == ScheduleStatus.CANCELLED.value


def test_a_member_may_look_but_not_cancel(client, campaign, member, contact):
    """Cancelling someone else's send is a lead decision -- the job belongs to
    another person's mailbox and may be half-sent."""
    job = schedule(campaign, member, [contact])
    sign_in(client, member)

    assert client.get(reverse("crm:schedule_list")).status_code == 200

    r = client.post(reverse("crm:schedule_cancel", args=[job.pk]))
    assert r.status_code in (302, 403)
    job.refresh_from_db()
    assert job.status == ScheduleStatus.PENDING.value
