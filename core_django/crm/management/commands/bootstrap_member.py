"""Make one real person able to send, on whatever database is configured.

`seed_dev` invents four fictional members. None of them can actually send mail:
the agent refuses to start unless AGENT_MEMBER_EMAIL matches both the API
token's owner and the Google account you sign in with, and nobody can sign in
as `ishita@pilani.bits-pilani.ac.in`.

This bridges that gap -- it creates the member for an address you genuinely
control, issues them a token, and (with --self-contact) gives them a prospect
that is themselves, so the first live send lands in your own inbox rather than
a stranger's.

    ../.venv/bin/python manage.py bootstrap_member you@pilani.bits-pilani.ac.in \
        --name "Your Name" --batch 2024 --self-contact

Re-runnable: the member is updated rather than duplicated, and every previous
token is revoked so a laptop that walked off cannot keep sending.
"""

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from crm.models import ApiToken, Campaign, Contact, TeamMember
from shared.enums import ContactLifecycle


class Command(BaseCommand):
    help = "Create a real, sendable team member and issue them an API token."

    def add_arguments(self, parser):
        parser.add_argument("email", help="Must be the Google account you can sign in as.")
        parser.add_argument("--name", default="", help="Display name. Defaults from the email.")
        parser.add_argument(
            "--batch",
            default="2024",
            help="2024 = lead (full control). 2025 = member (own contacts only).",
        )
        parser.add_argument("--password", default="devpassword", help="Django login password.")
        parser.add_argument(
            "--self-contact",
            action="store_true",
            help="Also create a contact addressed to this same person, assigned "
                 "to them, so the first live send is to your own inbox.",
        )

    @transaction.atomic
    def handle(self, *args, **opts):
        email = opts["email"].strip().lower()
        if "@" not in email:
            raise CommandError(f"{email!r} is not an email address.")

        name = opts["name"] or email.split("@")[0].replace(".", " ").title()

        user, _ = User.objects.get_or_create(
            username=email.split("@")[0], defaults={"email": email}
        )
        user.email = email
        user.set_password(opts["password"])
        user.save()

        member, created = TeamMember.objects.update_or_create(
            bits_email=email,
            defaults={"name": name, "batch": opts["batch"], "user": user, "is_active": True},
        )
        self.stdout.write(
            f"member   : {member.name} <{member.bits_email}> "
            f"batch {member.batch} ({'created' if created else 'updated'})"
        )

        # Anything issued earlier is now unaccounted for -- a re-run usually
        # means the old token was lost, and a lost token is a live credential.
        stale = member.api_tokens.filter(revoked_at__isnull=True).count()
        if stale:
            member.api_tokens.filter(revoked_at__isnull=True).update(
                revoked_at=timezone.now(), updated_at=timezone.now()
            )
            self.stdout.write(self.style.WARNING(f"revoked  : {stale} earlier token(s)"))

        _, raw = ApiToken.issue(member, label="bootstrap_member")

        if opts["self_contact"]:
            contact, _ = Contact.objects.update_or_create(
                email=email,
                defaults={
                    "first_name": name.split()[0],
                    "last_name": " ".join(name.split()[1:]) or "Test",
                    "company": "PIEDS",
                    "designation": "Test Recipient",
                    "assigned_to": member,
                    "created_by": member,
                    "tags": ["test"],
                    # Explicitly reset: a re-run after a successful send would
                    # otherwise leave it 'contacted' and the next test send
                    # would be refused as already mailed.
                    "lifecycle": ContactLifecycle.NEW.value,
                    "is_archived": False,
                },
            )
            self.stdout.write(f"contact  : {contact.full_name} <{contact.email}> assigned to self")

            campaign = Campaign.objects.order_by("created_at").first()
            if campaign:
                self.stdout.write(f"campaign : {campaign.title} ({campaign.status})")
            else:
                self.stdout.write(self.style.WARNING("campaign : none exist — run seed_dev"))

        self.stdout.write(self.style.SUCCESS("\nPut these in .env:\n"))
        self.stdout.write(f"AGENT_MEMBER_EMAIL={email}")
        self.stdout.write(f"AGENT_API_TOKEN={raw}")
        self.stdout.write(
            self.style.WARNING("\nThe token is shown once. It is not recoverable.")
        )
