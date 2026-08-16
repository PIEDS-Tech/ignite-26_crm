"""Embedded links: escaping, validation, and what actually goes out.

The load-bearing test here is `test_to_html_escapes_everything_it_did_not_write`.
The campaign form accepts no HTML and we ship no sanitiser -- the only reason
that is safe is that to_html() escapes every byte it did not generate itself.
If that test fails, a campaign body is an XSS vector in the preview and an
injection vector in the mail.
"""

import pytest

from crm.forms import CampaignForm
from crm.models import Campaign, CampaignMailing, Contact, TeamMember
from crm.services import mailing as mailing_svc
from crm.services.render import render
from crm.services.richtext import extract_links, to_html, to_plain, validate_links
from shared.enums import CampaignStatus

pytestmark = pytest.mark.django_db


# ------------------------------------------------------------------ richtext
# These need no database, but the module-level marker is harmless and keeps the
# file uniform.

def test_extract_links_finds_label_and_url():
    assert extract_links("hi [book a call](https://cal.com/x) ok") == [
        ("book a call", "https://cal.com/x")
    ]


def test_to_plain_keeps_the_url_visible():
    """The text/plain part must not be lossy -- the fallback still has to work."""
    assert to_plain("want to [book a call](https://cal.com/x)?") == (
        "want to book a call (https://cal.com/x)?"
    )


def test_to_html_makes_an_anchor_out_of_the_words():
    html = to_html("want to [book a call](https://cal.com/x)?")
    assert '<a href="https://cal.com/x">book a call</a>' in html


def test_to_html_escapes_everything_it_did_not_write():
    html = to_html(
        "<script>alert(1)</script> R&D "
        '[click <b>me</b>](https://x.test/?a=1&b="2")'
    )

    # Surrounding prose.
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "R&amp;D" in html

    # The label, so a bold tag cannot be smuggled through the link text.
    assert "<b>me</b>" not in html
    assert "click &lt;b&gt;me&lt;/b&gt;" in html

    # The href, so a quote cannot close the attribute and open an event handler.
    assert 'href="https://x.test/?a=1&amp;b=&quot;2&quot;"' in html


def test_to_html_turns_newlines_into_breaks():
    assert "<br>" in to_html("one\ntwo")


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,<b>x</b>"])
def test_dangerous_schemes_are_rejected(url):
    problems = validate_links(f"[click]({url})")
    assert problems and "scheme" in problems[0]


def test_malformed_url_is_rejected():
    assert validate_links("[click](not a url)") or validate_links("[click](htp://x)")


def test_plain_https_link_passes():
    assert validate_links("[click](https://example.com/a?b=1)") == []


def test_placeholder_url_is_left_for_send_time():
    """`[site]({{ company }})` cannot be validated now; it resolves per contact."""
    assert validate_links("[site]({{ company }})") == []


def test_body_without_links_is_unchanged_as_plain_text():
    body = "Hi there,\n\nJust checking in.\n"
    assert to_plain(body) == body


# ---------------------------------------------------------------------- form

@pytest.fixture
def member():
    return TeamMember.objects.create(
        name="Kabir", bits_email="kabir@pilani.bits-pilani.ac.in", batch="2025"
    )


def _form_data(body, subject="Hello {{ first_name }}", variables="first_name"):
    return {
        "title": "Outreach",
        "mail_sub": subject,
        "mail_body": body,
        "var_list_raw": variables,
    }


def test_form_rejects_a_javascript_link():
    form = CampaignForm(data=_form_data("Hi {{ first_name }}, [click](javascript:alert(1))"))
    assert not form.is_valid()
    assert any("scheme" in e for e in form.errors["mail_body"])


def test_form_accepts_a_real_link():
    form = CampaignForm(
        data=_form_data("Hi {{ first_name }}, [book a call](https://cal.com/x).")
    )
    assert form.is_valid(), form.errors


def test_form_checks_links_in_the_subject_too():
    form = CampaignForm(
        data=_form_data("Hi {{ first_name }}.", subject="[x](javascript:alert(1))")
    )
    assert not form.is_valid()


# -------------------------------------------------------------------- render

@pytest.fixture
def campaign(member):
    return Campaign.objects.create(
        title="Outreach",
        mail_sub="{{ company }} x PIEDS",
        mail_body="Hi {{ first_name }}, want to [book a call](https://cal.com/x)?",
        var_list=["company", "first_name"],
        status=CampaignStatus.ACTIVE.value,
        created_by=member,
    )


@pytest.fixture
def contact(member):
    return Contact.objects.create(
        first_name="Rohan",
        last_name="Iyer",
        email="rohan@example.com",
        company="Acme & Co",
        designation="CTO",
        assigned_to=member,
        created_by=member,
    )


def test_render_produces_both_bodies(campaign, contact):
    rendered = render(campaign, contact)

    assert rendered.body == "Hi Rohan, want to book a call (https://cal.com/x)?"
    assert '<a href="https://cal.com/x">book a call</a>' in rendered.body_html
    assert "Hi Rohan," in rendered.body_html


def test_contact_data_is_escaped_into_the_html(campaign, contact):
    """Substitute-then-convert: an ampersand in a company name must not break markup."""
    campaign.mail_body = "Hi {{ first_name }} at {{ company }}."
    rendered = render(campaign, contact)

    assert "Acme &amp; Co" in rendered.body_html
    assert "Acme & Co" in rendered.body          # plain text keeps it raw
    assert "Acme & Co." not in rendered.body_html


def test_claim_snapshots_the_html_and_returns_it(campaign, contact, member):
    claimed, skipped = mailing_svc.claim_batch(campaign, member, [contact.id])
    assert not skipped and len(claimed) == 1

    assert '<a href="https://cal.com/x">' in claimed[0].body_html

    mailing = CampaignMailing.objects.get(campaign=campaign, contact=contact)
    assert mailing.rendered_body_html == claimed[0].body_html
    assert mailing.rendered_body == claimed[0].body
