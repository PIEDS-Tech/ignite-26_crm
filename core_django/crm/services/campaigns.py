"""Campaign lifecycle rules, imposed at the service layer (not on the model).

The model stores `status` as a plain char field with choices; nothing stops a
bad value being written by a careless `.save()`. Every intentional change goes
through `transition()`, which is where the actual rules live.
"""

import re

from django.core.exceptions import ValidationError

from shared.enums import CampaignStatus

#: What may follow what. Terminal states have no outgoing edges except archive.
ALLOWED_TRANSITIONS = {
    CampaignStatus.DRAFT: {CampaignStatus.ACTIVE, CampaignStatus.ARCHIVED},
    CampaignStatus.ACTIVE: {CampaignStatus.PAUSED, CampaignStatus.COMPLETED},
    CampaignStatus.PAUSED: {CampaignStatus.ACTIVE, CampaignStatus.COMPLETED},
    CampaignStatus.COMPLETED: {CampaignStatus.ARCHIVED},
    CampaignStatus.ARCHIVED: set(),
}

#: Matches {{ var }} / {{var}} in subject and body.
PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")

#: Contact fields a template is allowed to interpolate.
ALLOWED_VARIABLES = {
    "first_name",
    "last_name",
    "full_name",
    "email",
    "company",
    "designation",
}


def extract_placeholders(campaign) -> set[str]:
    text = f"{campaign.mail_sub}\n{campaign.mail_body}"
    return set(PLACEHOLDER_RE.findall(text))


def validate_template(campaign) -> None:
    """Every placeholder must be declared in var_list AND be a real field.

    Run before allowing DRAFT -> ACTIVE. Catching a typo like {{ compnay }}
    here is the difference between a clean campaign and 200 mails that say
    "Dear {{ first_name }}".
    """
    used = extract_placeholders(campaign)
    declared = set(campaign.var_list or [])

    unknown = used - ALLOWED_VARIABLES
    if unknown:
        raise ValidationError(
            "Template uses variables that are not Contact fields: "
            + ", ".join(sorted(unknown))
            + ". Allowed: "
            + ", ".join(sorted(ALLOWED_VARIABLES))
        )

    undeclared = used - declared
    if undeclared:
        raise ValidationError(
            "Template uses variables missing from var_list: " + ", ".join(sorted(undeclared))
        )

    unused = declared - used
    if unused:
        raise ValidationError(
            "var_list declares variables the template never uses: "
            + ", ".join(sorted(unused))
        )


def transition(campaign, to_status) -> None:
    """Move a campaign to a new status, or raise ValidationError."""
    current = CampaignStatus(campaign.status)
    target = CampaignStatus(to_status)

    if current == target:
        return

    if target not in ALLOWED_TRANSITIONS[current]:
        allowed = ", ".join(sorted(s.value for s in ALLOWED_TRANSITIONS[current])) or "nothing"
        raise ValidationError(
            f"Cannot move campaign from {current.value} to {target.value}. "
            f"Allowed from {current.value}: {allowed}."
        )

    # Going live is the only transition that lets mail leave the building, so
    # it is the only one that gets a template check.
    if target == CampaignStatus.ACTIVE:
        validate_template(campaign)

    campaign.status = target.value
    campaign.save(update_fields=["status", "updated_at"])
