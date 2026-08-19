from django.contrib import admin

from .models import (
    Campaign,
    CampaignMailing,
    Contact,
    ContactNote,
    FollowUpRule,
    ScheduledSend,
    TeamMember,
)


@admin.register(TeamMember)
class TeamMemberAdmin(admin.ModelAdmin):
    list_display = ("name", "bits_email", "batch", "is_active", "contact_count")
    list_filter = ("batch", "is_active")
    search_fields = ("name", "bits_email")

    @admin.display(description="assigned")
    def contact_count(self, obj):
        return obj.assigned_contacts.count()


class ContactNoteInline(admin.TabularInline):
    model = ContactNote
    extra = 0
    readonly_fields = ("created_at",)


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = (
        "full_name",
        "email",
        "company",
        "designation",
        "assigned_to",
        "last_contacted_at",
    )
    list_filter = ("assigned_to", "company")
    search_fields = ("first_name", "last_name", "email", "company")
    autocomplete_fields = ("assigned_to", "last_contacted_by")
    inlines = [ContactNoteInline]


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ("title", "status", "created_by", "mailing_count", "created_at")
    list_filter = ("status",)
    search_fields = ("title", "mail_sub")

    @admin.display(description="mailings")
    def mailing_count(self, obj):
        return obj.mailings.count()


@admin.register(CampaignMailing)
class CampaignMailingAdmin(admin.ModelAdmin):
    list_display = ("campaign", "contact", "sent_by", "status", "sent_at", "mail_thread_id")
    list_filter = ("status", "campaign", "sent_by")
    search_fields = ("contact__email", "mail_thread_id")
    readonly_fields = ("rendered_subject", "rendered_body", "error_detail")


@admin.register(ScheduledSend)
class ScheduledSendAdmin(admin.ModelAdmin):
    """Read-mostly. Cancelling belongs on /schedules/, which explains itself and
    says out loud that mail already sent stays sent."""

    list_display = ("campaign", "member", "scheduled_at", "status", "progress", "next_run_at")
    list_filter = ("status", "campaign", "member")
    readonly_fields = ("cursor", "sent_count", "skipped_count", "attempts",
                       "leased_by", "lease_expires_at", "started_at", "finished_at")

    @admin.display(description="progress")
    def progress(self, obj):
        return f"{obj.cursor}/{obj.total}"


@admin.register(FollowUpRule)
class FollowUpRuleAdmin(admin.ModelAdmin):
    list_display = ("campaign", "follow_up", "delay_days", "mark_replied", "is_active")
    list_filter = ("is_active", "mark_replied")
    autocomplete_fields = ("campaign", "follow_up")
