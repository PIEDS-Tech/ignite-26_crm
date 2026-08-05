from django.urls import path

from . import views

app_name = "api"

urlpatterns = [
    path("me", views.me, name="me"),
    path("campaigns", views.campaigns, name="campaigns"),
    path("contacts", views.contacts, name="contacts"),
    path("mailings/preflight", views.preflight, name="preflight"),
    path("mailings/claim", views.claim, name="claim"),
    path("mailings/drafts", views.drafts, name="drafts"),
    path("mailings/<uuid:mailing_id>/result", views.report_result, name="result"),
]
