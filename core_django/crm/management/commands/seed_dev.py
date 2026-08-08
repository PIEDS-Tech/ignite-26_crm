"""Populate a dev database with realistic-looking data.

Idempotent: re-running updates rather than duplicating, so it is safe to call
repeatedly while iterating.
"""

import random

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction

from crm.models import Campaign, Contact, TeamMember
from shared.enums import CampaignStatus, ContactLifecycle

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

    @transaction.atomic
    def handle(self, *args, **opts):
        random.seed(42)

        members = []
        for name, email, batch, phone in MEMBERS:
            user, _ = User.objects.get_or_create(
                username=email.split("@")[0], defaults={"email": email}
            )
            user.set_password("devpassword")
            user.save()

            member, _ = TeamMember.objects.update_or_create(
                bits_email=email,
                defaults={"name": name, "batch": batch, "phone": phone, "user": user},
            )
            members.append(member)
        self.stdout.write(f"team members: {len(members)} (password: devpassword)")

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
