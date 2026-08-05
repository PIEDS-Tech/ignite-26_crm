"""Canonical schema for Ignite CRM.

This module is the SINGLE SOURCE OF TRUTH for the database. The FastAPI local
agent mirrors these tables in `local_agent/db/models.py` via SQLAlchemy and
never runs migrations of its own -- it verifies at startup that its mirror
still matches what lives here.
"""

import hashlib
import secrets
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from shared.enums import CampaignStatus, MailingStatus

from .validators import batch_validator, phone_validator, validate_bits_email


class TimeStampedModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class TeamMember(TimeStampedModel):
    """A PIEDS team member who sends mail from their own Gmail account."""

    name = models.CharField(max_length=120)
    bits_email = models.EmailField(
        unique=True,
        validators=[validate_bits_email],
        help_text="Must match the Gmail account used by the local sending agent.",
    )
    phone = models.CharField(max_length=10, validators=[phone_validator], blank=True)
    linkedin = models.URLField(blank=True)
    batch = models.CharField(
        max_length=4,
        validators=[batch_validator],
        db_index=True,
        help_text="Admission year. Drives permissions -- see services/permissions.py.",
    )
    is_active = models.BooleanField(default=True)

    # Links this member to a Django login for the central admin UI. Null for
    # members who only ever run the local agent and never log into Django.
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="team_member",
    )

    class Meta:
        db_table = "team_members"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.batch})"


class ApiToken(TimeStampedModel):
    """Bearer token letting a member's local agent talk to this server.

    Only the hash is stored. The plaintext is shown once at creation and is
    unrecoverable afterwards -- a leaked database should not hand an attacker
    working credentials for every member's agent.
    """

    member = models.ForeignKey(TeamMember, on_delete=models.CASCADE, related_name="api_tokens")
    label = models.CharField(max_length=80, blank=True, help_text="e.g. 'Aarav's MacBook'")
    key_hash = models.CharField(max_length=64, unique=True, db_index=True)
    key_prefix = models.CharField(max_length=8, help_text="First chars, shown in the UI.")
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "api_tokens"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.key_prefix}… ({self.member.name})"

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None and self.member.is_active

    @staticmethod
    def hash_key(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def issue(cls, member, label: str = "") -> tuple["ApiToken", str]:
        """Create a token. Returns (token, plaintext) -- show plaintext once."""
        raw = secrets.token_urlsafe(32)
        token = cls.objects.create(
            member=member, label=label, key_hash=cls.hash_key(raw), key_prefix=raw[:8]
        )
        return token, raw

    def revoke(self):
        self.revoked_at = timezone.now()
        self.save(update_fields=["revoked_at", "updated_at"])


class Contact(TimeStampedModel):
    """A prospect. Owned by at most one team member at a time."""

    first_name = models.CharField(max_length=80)
    last_name = models.CharField(max_length=80, blank=True)
    email = models.EmailField(
        unique=True,
        help_text="Unique -- this is the dedupe key for CSV import.",
    )
    phone_no = models.CharField(max_length=10, validators=[phone_validator], blank=True)
    linkedin = models.URLField(blank=True)
    company = models.CharField(max_length=160, db_index=True)
    designation = models.CharField(max_length=160, blank=True)

    assigned_to = models.ForeignKey(
        TeamMember,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_contacts",
    )
    assigned_at = models.DateTimeField(null=True, blank=True)

    last_contacted_by = models.ForeignKey(
        TeamMember,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    last_contacted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "contacts"
        ordering = ["company", "first_name"]
        indexes = [models.Index(fields=["assigned_to", "company"])]

    def __str__(self):
        return f"{self.full_name} @ {self.company}"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()


class ContactNote(TimeStampedModel):
    """Append-only note on a contact.

    A separate table rather than a text column so we keep who wrote what and
    when -- on a shared contact pool that history is the whole point.
    """

    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="notes")
    author = models.ForeignKey(TeamMember, on_delete=models.SET_NULL, null=True, related_name="+")
    body = models.TextField()

    class Meta:
        db_table = "contact_notes"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Note on {self.contact_id} by {self.author_id}"


class Campaign(TimeStampedModel):
    """A reusable mail template plus its lifecycle state.

    Status transitions are NOT enforced here -- see services/campaigns.py.
    """

    title = models.CharField(max_length=200)
    mail_sub = models.CharField(max_length=300, help_text="Supports {{ variable }} placeholders.")
    mail_body = models.TextField(help_text="Supports {{ variable }} placeholders.")
    var_list = models.JSONField(
        default=list,
        blank=True,
        help_text='Declared variables, e.g. ["first_name", "company", "designation"].',
    )
    status = models.CharField(
        max_length=16,
        choices=CampaignStatus.choices(),
        default=CampaignStatus.DRAFT.value,
        db_index=True,
    )
    created_by = models.ForeignKey(
        TeamMember, on_delete=models.SET_NULL, null=True, related_name="campaigns"
    )

    class Meta:
        db_table = "campaigns"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} [{self.status}]"


class CampaignMailing(TimeStampedModel):
    """One mail, to one contact, for one campaign. The unit of idempotency."""

    campaign = models.ForeignKey(Campaign, on_delete=models.PROTECT, related_name="mailings")
    contact = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name="mailings")
    sent_by = models.ForeignKey(TeamMember, on_delete=models.PROTECT, related_name="mailings")

    mail_thread_id = models.CharField(max_length=120, blank=True)
    mail_message_id = models.CharField(max_length=120, blank=True)

    status = models.CharField(
        max_length=8,
        choices=MailingStatus.choices(),
        default=MailingStatus.DRAFT.value,
    )

    # Snapshot of what actually went out. Campaign templates change over time;
    # without this we could never answer "what did we actually send this person".
    rendered_subject = models.TextField(blank=True)
    rendered_body = models.TextField(blank=True)

    error_detail = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "campaign_mailings"
        ordering = ["-created_at"]
        constraints = [
            # THE idempotency guarantee. A double-click, a retry, or two agents
            # racing all collide here at the database rather than putting a
            # second copy of the same mail in a prospect's inbox.
            models.UniqueConstraint(
                fields=["campaign", "contact"], name="uniq_campaign_contact"
            )
        ]
        indexes = [
            models.Index(fields=["campaign", "status"]),
            models.Index(fields=["sent_by", "sent_at"]),
        ]

    def __str__(self):
        return f"{self.campaign_id} -> {self.contact_id} [{self.status}]"
