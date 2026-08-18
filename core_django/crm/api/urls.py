from django.urls import path

from . import views

app_name = "api"

urlpatterns = [
    path("me", views.me, name="me"),
    path("campaigns", views.campaigns, name="campaigns"),
    path("contacts", views.contacts, name="contacts"),
    path("contacts/new", views.contact_create, name="contact_create"),
    path("contacts/<uuid:contact_id>", views.contact_update, name="contact_update"),
    path("mailings/preflight", views.preflight, name="preflight"),
    path("mailings/claim", views.claim, name="claim"),
    path("mailings/drafts", views.drafts, name="drafts"),
    path("mailings/<uuid:mailing_id>/result", views.report_result, name="result"),

    # Scheduled sends. `claim` leases what is due for the calling agent's
    # member; see docs/MAIL_SCHEDULING.md for the lease protocol.
    path("schedules", views.schedules, name="schedules"),
    path("schedules/claim", views.schedule_claim, name="schedule_claim"),
    path("schedules/<uuid:schedule_id>/progress", views.schedule_progress,
         name="schedule_progress"),
    path("schedules/<uuid:schedule_id>/cancel", views.schedule_cancel,
         name="schedule_cancel"),
    path("schedules/<uuid:schedule_id>/reschedule", views.schedule_reschedule,
         name="schedule_reschedule"),
]
