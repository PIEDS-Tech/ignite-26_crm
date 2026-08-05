"""Bulk assignment of contacts to team members."""

from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from crm.models import Contact


@dataclass
class AssignmentResult:
    assigned: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (email, reason)


@transaction.atomic
def bulk_assign(contact_ids, member, *, force: bool = False) -> AssignmentResult:
    """Assign contacts to `member`.

    Reassigning a contact who already has mailings is refused unless `force`:
    the existing owner may be mid-conversation, and silently moving the contact
    would strand that thread with no owner watching for the reply.
    """
    if not member.is_active:
        raise ValidationError(f"{member.name} is not an active team member.")

    result = AssignmentResult()
    contacts = Contact.objects.select_for_update().filter(id__in=contact_ids)

    for contact in contacts:
        if contact.assigned_to_id == member.id:
            result.skipped.append((contact.email, "already assigned to this member"))
            continue

        if not force and contact.assigned_to_id and contact.mailings.exists():
            result.skipped.append(
                (contact.email, "has mail history under another member; use force to override")
            )
            continue

        contact.assigned_to = member
        contact.assigned_at = timezone.now()
        contact.save(update_fields=["assigned_to", "assigned_at", "updated_at"])
        result.assigned += 1

    return result


@transaction.atomic
def bulk_unassign(contact_ids) -> int:
    return Contact.objects.filter(id__in=contact_ids).update(
        assigned_to=None, assigned_at=None, updated_at=timezone.now()
    )
