from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator

#: Indian mobile numbers: 10 digits starting 6-9. Stored without country code.
phone_validator = RegexValidator(
    regex=r"^[6-9]\d{9}$",
    message="Enter a valid 10-digit Indian mobile number (no +91, no spaces).",
)

#: Batch is a 4-digit admission year, e.g. "2024".
batch_validator = RegexValidator(
    regex=r"^20\d{2}$",
    message="Batch must be a 4-digit year like 2024.",
)

BITS_EMAIL_DOMAINS = (
    "pilani.bits-pilani.ac.in",
    "goa.bits-pilani.ac.in",
    "hyderabad.bits-pilani.ac.in",
    "dubai.bits-pilani.ac.in",
    "bits-pilani.ac.in",
)


def validate_bits_email(value: str) -> None:
    """Team members must use an institute address.

    `sent_by` attribution is only meaningful if members are who they claim to
    be, and the local agent binds the authenticated Gmail account to this
    field. Allowing arbitrary domains would let anyone register as a member.
    """
    domain = value.rsplit("@", 1)[-1].lower()
    if domain not in BITS_EMAIL_DOMAINS:
        raise ValidationError(
            f"{value!r} is not a BITS address. Allowed domains: "
            + ", ".join(BITS_EMAIL_DOMAINS)
        )
