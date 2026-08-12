"""Issue an API token from the command line.

The UI equivalent lives on /members/ and is the normal path. This exists for
bootstrapping -- a containerised agent needs a token before anyone can log in
and click anything, and the plaintext key is shown exactly once either way.
"""

from django.core.management.base import BaseCommand, CommandError

from crm.models import ApiToken, TeamMember


class Command(BaseCommand):
    help = "Issue an API token for a team member. The key is printed once."

    def add_arguments(self, parser):
        parser.add_argument("email", help="the member's BITS address")
        parser.add_argument("--label", default="cli", help="e.g. 'Aarav's MacBook'")

    def handle(self, *args, **options):
        try:
            member = TeamMember.objects.get(bits_email__iexact=options["email"])
        except TeamMember.DoesNotExist:
            raise CommandError(f"No team member with email {options['email']!r}.")

        _, raw = ApiToken.issue(member, label=options["label"])

        self.stdout.write(self.style.SUCCESS("Token issued. Copy it into .env now:"))
        self.stdout.write(f"AGENT_MEMBER_EMAIL={member.bits_email}")
        self.stdout.write(f"AGENT_API_TOKEN={raw}")
        self.stdout.write(
            self.style.WARNING("Only the hash is stored -- this will not be shown again.")
        )
