from django.contrib import admin

from .models import Campaign, CampaignMailing, Contact, ContactNote, TeamMember


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
