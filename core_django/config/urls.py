from django.contrib import admin
from django.urls import include, path

from crm import auth_views

urlpatterns = [
    # Django's own auth, for superusers only. The CRM does not use it.
    path("admin/", admin.site.urls),

    path("login/", auth_views.login_page, name="login"),
    path("login/name/", auth_views.login_by_name, name="login_by_name"),
    path("login/google/", auth_views.google_login, name="google_login"),
    path("login/google/callback/", auth_views.google_callback, name="google_callback"),
    path("logout/", auth_views.logout_view, name="logout"),
    path("api/v1/", include("crm.api.urls")),
    path("", include("crm.urls")),
]
