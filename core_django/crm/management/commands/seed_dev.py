"""Populate a dev database with realistic-looking data.

Idempotent: re-running updates rather than duplicating, so it is safe to call
repeatedly while iterating.
"""

import random

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from crm.models import Campaign, Contact, TeamMember
from shared.enums import LEAD_BATCH, CampaignStatus, ContactLifecycle

#: Seeding writes fixture contacts and an active campaign. Doing that to the
#: database the whole team shares would put invented prospects in a real pool --
#: and `update_or_create` on `title` means it would happily overwrite a live
#: campaign's template. The entrypoint decides this too; the check lives here as
#: well because a person typing the command by hand deserves the same guard.
LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1", "postgres"}

MEMBERS = [
    ("Aarav Sharma", "aarav@pilani.bits-pilani.ac.in", "2024", "9812345670"),
    ("Diya Menon", "diya@pilani.bits-pilani.ac.in", "2024", "9812345671"),
    ("Kabir Rao", "kabir@pilani.bits-pilani.ac.in", "2025", "9812345672"),
    ("Ishita Nair", "ishita@pilani.bits-pilani.ac.in", "2025", "9812345673"),
]

COMPANIES = [
    "Zerodha", "Razorpay", "Postman", "Freshworks", "Zoho", "CRED", "Groww",
    "Meesho", "Innovaccer", "Darwinbox", "Chargebee", "BrowserStack",
]
DESIGNATIONS = ["Founder", "CTO", "VP Engineering", "Head of Partnerships", "Director, Strategy"]
FIRST = ["Rohan", "Ananya", "Vikram", "Sneha", "Arjun", "Meera", "Nikhil", "Priya", "Rahul", "Tara"]
LAST = ["Iyer", "Kapoor", "Reddy", "Bose", "Desai", "Malhotra", "Pillai", "Chawla"]
TAGS = ["fintech", "saas", "deeptech", "priority", "warm-intro", "iit-b", "alum"]


class Command(BaseCommand):
    help = "Seed the dev database with team members, contacts and a campaign."

    def add_arguments(self, parser):
        parser.add_argument("--contacts", type=int, default=50)
        parser.add_argument(
            "--force",
            action="store_true",
            help="Seed even when the database is not local. Almost never right.",
        )

    @transaction.atomic
    def handle(self, *args, **opts):
        host = (settings.DATABASES["default"].get("HOST") or "").lower()
        if host not in LOCAL_HOSTS and not opts["force"]:
            raise CommandError(
                f"Refusing to seed {host!r} -- that is not a local database. "
                f"Seeding writes fixture contacts and overwrites the campaign "
                f"template of the same title. Pass --force if you are certain."
            )

        random.seed(42)

        members = []
        for name, email, batch, phone in MEMBERS:
            member, _ = TeamMember.objects.update_or_create(
                bits_email=email,
                defaults={"name": name, "batch": batch, "phone": phone},
            )
            members.append(member)

        # No Django Users: the CRM signs people in by name (batch 2024) or with
        # Google (batch 2025). `manage.py createsuperuser` is for /admin/ only.
        leads = [m.name for m in members if m.batch == LEAD_BATCH]
        self.stdout.write(
            f"team members: {len(members)} — sign in by picking a name: {', '.join(leads)}"
        )

        assignees = [m for m in members if m.batch == "2025"]
        created = 0
        for i in range(opts["contacts"]):
            first, last = random.choice(FIRST), random.choice(LAST)
            email = f"{first.lower()}.{last.lower()}{i}@example.com"
            _, was_new = Contact.objects.update_or_create(
                email=email,
                defaults={
                    "first_name": first,
                    "last_name": last,
                    "company": random.choice(COMPANIES),
                    "designation": random.choice(DESIGNATIONS),
                    # Leave a third unassigned so the assignment screen has work to do.
                    "assigned_to": random.choice(assignees + [None]),
                    "tags": random.sample(TAGS, random.randint(0, 2)),
                    # A couple of blocked contacts so the "refuses to mail"
                    # path is visible without having to set one up by hand.
                    "lifecycle": random.choices(
                        [ContactLifecycle.NEW.value,
                         ContactLifecycle.REPLIED.value,
                         ContactLifecycle.DO_NOT_CONTACT.value],
                        weights=[88, 8, 4],
                    )[0],
                },
            )
            created += was_new
        self.stdout.write(f"contacts: {created} new, {Contact.objects.count()} total")

        campaign, _ = Campaign.objects.update_or_create(
            title="PIEDS Incubation Outreach — Spring",
            defaults={
                "mail_sub": "{{ company }} x PIEDS, BITS Pilani",
                "mail_body": (
                    "Hi {{ first_name }},\n\n"
                    "I'm reaching out from PIEDS, the technology business incubator at "
                    "BITS Pilani. Given your work as {{ designation }} at {{ company }}, "
                    "I thought there might be a good fit worth exploring.\n\n"
                    "Would you be open to a short call next week?\n\n"
                    "Best,\nPIEDS Team"
                ),
                "var_list": ["first_name", "company", "designation"],
                "status": CampaignStatus.ACTIVE.value,
                "created_by": members[0],
            },
        )
        self.stdout.write(self.style.SUCCESS(f"campaign ready: {campaign.title}"))
