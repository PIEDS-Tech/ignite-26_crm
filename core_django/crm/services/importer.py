"""CSV contact import: parse -> validate -> preview -> commit.

Deliberately two-step. A contact list is usually someone's hand-built
spreadsheet, and finding out it was malformed *after* half of it landed in the
shared pool is much worse than being made to look at a preview first.
"""

import csv
import io
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db import transaction

from crm.models import Contact

REQUIRED_COLUMNS = {"first_name", "email", "company"}
OPTIONAL_COLUMNS = {"last_name", "phone_no", "linkedin", "designation"}
KNOWN_COLUMNS = REQUIRED_COLUMNS | OPTIONAL_COLUMNS

NEW = "new"
DUPLICATE = "duplicate"
INVALID = "invalid"


@dataclass
class Row:
    line: int
    data: dict
    state: str
    reason: str = ""

    @property
    def is_importable(self) -> bool:
        return self.state == NEW


@dataclass
class ImportPreview:
    rows: list[Row] = field(default_factory=list)
    header_error: str = ""

    @property
    def ok(self) -> bool:
        return not self.header_error

    def count(self, state: str) -> int:
        return sum(1 for r in self.rows if r.state == state)

    # Django templates can't pass arguments, so expose the three as properties.
    @property
    def count_new(self) -> int:
        return self.count(NEW)

    @property
    def count_duplicate(self) -> int:
        return self.count(DUPLICATE)

    @property
    def count_invalid(self) -> int:
        return self.count(INVALID)

    @property
    def importable(self) -> list[Row]:
        return [r for r in self.rows if r.is_importable]


def parse(file_bytes: bytes) -> ImportPreview:
    """Validate every row without touching the database."""
    preview = ImportPreview()

    try:
        text = file_bytes.decode("utf-8-sig")   # Excel loves a BOM
    except UnicodeDecodeError:
        preview.header_error = "File is not valid UTF-8. Re-export it as UTF-8 CSV."
        return preview

    reader = csv.DictReader(io.StringIO(text))
    headers = {h.strip().lower() for h in (reader.fieldnames or [])}

    missing = REQUIRED_COLUMNS - headers
    if missing:
        preview.header_error = (
            "Missing required column(s): " + ", ".join(sorted(missing))
            + ". Expected headers: " + ", ".join(sorted(KNOWN_COLUMNS))
        )
        return preview

    existing_emails = set(
        Contact.objects.values_list("email", flat=True)
    )
    seen_in_file: set[str] = set()

    for line, raw in enumerate(reader, start=2):    # line 1 is the header
        data = {
            key: (raw.get(key) or "").strip()
            for key in KNOWN_COLUMNS
        }
        email = data["email"].lower()
        data["email"] = email

        if not email:
            preview.rows.append(Row(line, data, INVALID, "email is blank"))
            continue
        if email in existing_emails:
            preview.rows.append(Row(line, data, DUPLICATE, "already in the contact pool"))
            continue
        if email in seen_in_file:
            preview.rows.append(Row(line, data, DUPLICATE, "repeated earlier in this file"))
            continue

        # Reuse the model's own validators rather than re-deriving the rules.
        contact = Contact(**data)
        try:
            contact.full_clean(exclude=["assigned_to", "last_contacted_by"])
        except ValidationError as exc:
            reason = "; ".join(
                f"{f}: {' '.join(msgs)}" for f, msgs in exc.message_dict.items()
            )
            preview.rows.append(Row(line, data, INVALID, reason))
            continue

        seen_in_file.add(email)
        preview.rows.append(Row(line, data, NEW))

    return preview


@transaction.atomic
def commit(preview: ImportPreview) -> int:
    """Insert every importable row, all-or-nothing."""
    contacts = [Contact(**row.data) for row in preview.importable]
    Contact.objects.bulk_create(contacts)
    return len(contacts)
