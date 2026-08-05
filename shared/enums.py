"""Status vocabularies shared by core_django and local_agent.

These strings are persisted in Postgres, so both apps MUST agree on them
character for character. Defining them once here is the only thing that
prevents the Django models and the SQLAlchemy mirror from drifting.

Plain `str` subclasses so `CampaignStatus.ACTIVE == "active"` is True and the
values drop straight into Django `choices` and SQLAlchemy comparisons.
"""

from enum import Enum


class CampaignStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ARCHIVED = "archived"

    @classmethod
    def choices(cls):
        return [(m.value, m.name.title()) for m in cls]


class MailingStatus(str, Enum):
    DRAFT = "draft"
    SENT = "sent"
    FAILED = "failed"

    @classmethod
    def choices(cls):
        return [(m.value, m.name.title()) for m in cls]


#: Campaigns may only be mailed from in this state. Enforced by the local agent
#: before any send, and by the service layer on transition.
SENDABLE_CAMPAIGN_STATUSES = frozenset({CampaignStatus.ACTIVE})

#: The batch that may access the assignment UI. Single source of truth for the
#: permission rule -- never inline this literal anywhere else.
LEAD_BATCH = "2024"
