"""Report mailings claimed but never resolved, and who has to fix them.

A DRAFT means "an agent reserved this contact and we never heard back". It is
not a mail in flight; nothing is going to happen to it on its own. Until it is
resolved the contact CANNOT be mailed for that campaign again -- which is how a
dropped connection turns into a campaign that silently stops working.

Resolving requires Gmail, which this server deliberately does not have. So this
command reports; the member's own agent resolves, with "Resolve stranded
drafts". See docs/MAIL_SCHEDULING.md and README §7.
"""

from django.core.management.base import BaseCommand
from django.db.models import Count, Max, Min

from crm.models import CampaignMailing
from shared.enums import MailingStatus


class Command(BaseCommand):
    help = "Show mailings stuck in DRAFT, grouped by the member who must resolve them."

    def add_arguments(self, parser):
        parser.add_argument(
            "--campaign", help="Limit to one campaign id.", default=None,
        )

    def handle(self, *args, **options):
        qs = CampaignMailing.objects.filter(status=MailingStatus.DRAFT.value)
        if options["campaign"]:
            qs = qs.filter(campaign_id=options["campaign"])

        total = qs.count()
        if not total:
            self.stdout.write(self.style.SUCCESS("No stranded drafts. Nothing to do."))
            return

        self.stdout.write(self.style.WARNING(f"{total} stranded draft(s).\n"))
        self.stdout.write(
            "These contacts cannot be mailed for their campaign until each is\n"
            "resolved against Gmail, because a claimed contact cannot be claimed\n"
            "again. Nothing here is in flight.\n"
        )

        rows = (
            qs.values("sent_by__name", "sent_by__bits_email", "campaign__title")
            .annotate(n=Count("id"), first=Min("created_at"), last=Max("created_at"))
            .order_by("-n")
        )

        self.stdout.write(f"\n{'member':16} {'campaign':34} {'count':>6}  claimed")
        for r in rows:
            self.stdout.write(
                f"{(r['sent_by__name'] or '?')[:16]:16} "
                f"{(r['campaign__title'] or '?')[:34]:34} "
                f"{r['n']:>6}  {r['first']:%d %b %H:%M} - {r['last']:%d %b %H:%M}"
            )

        members = sorted({r["sent_by__name"] for r in rows if r["sent_by__name"]})
        self.stdout.write(
            self.style.MIGRATE_HEADING("\nHow to clear this\n")
            + "Each member below opens their own local agent and presses\n"
            + '"Resolve stranded drafts". That asks Gmail whether each mail\n'
            + "actually went out:\n\n"
            + "  - already sent  -> recorded as sent, nobody is mailed twice\n"
            + "  - never sent    -> marked failed, and can then be sent again\n\n"
            + "Then re-select those contacts and send. Only that member's own\n"
            + "agent can do it -- the mail left their mailbox, and only they\n"
            + "hold the Gmail credentials to check it.\n\n"
            + "Waiting on: " + ", ".join(members)
        )
