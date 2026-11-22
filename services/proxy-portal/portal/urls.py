from django.urls import path, register_converter
from django.views.i18n import set_language

from . import admin_views, self_reset, views


class RequestId:
    regex = "[a-z0-9][a-z0-9-]{7,63}"

    def to_python(self, value):
        return value

    def to_url(self, value):
        return value


register_converter(RequestId, "rid")

# 500 is Django's own view with the standalone templates/500.html, which needs no request
handler404 = views.not_found

urlpatterns = [
    path("", views.status_page, name="status"),
    path("access", views.access_page, name="access"),
    path("usage", views.usage_page, name="usage"),
    path("signin", views.signin, name="signin"),
    path("signout", views.signout, name="signout"),
    path("language", set_language, name="set_language"),
    path("api/status", views.api_status, name="api_status"),
    path("api/status/live", views.api_status_live, name="api_status_live"),
    path("api/me/access", views.api_me_access, name="api_me_access"),
    path("api/me/usage", views.api_me_usage, name="api_me_usage"),
    path("api/me/reset", self_reset.api_reset, name="api_me_reset"),
    path("api/me/reset/<rid:request_id>", self_reset.api_reset_state, name="api_me_reset_state"),
    path("api/me/reset/<rid:request_id>/password", self_reset.api_reset_password, name="api_me_reset_password"),
    path("healthz", views.healthz, name="healthz"),
    # the admin console: every view checks the Determined admin flag of the session (admin_views.admin_only)
    path("admin/users", admin_views.users_page, name="admin_users"),
    path("admin/proxies", admin_views.proxies_page, name="admin_proxies"),
    path("admin/accounts", admin_views.accounts_page, name="admin_accounts"),
    path("admin/accounts/<str:op>", admin_views.confirm_page, name="admin_confirm"),
    path("admin/requests/<rid:request_id>", admin_views.request_page, name="admin_request"),
    path("admin/requests/<rid:request_id>/password", admin_views.password_page, name="admin_password"),
    path("admin/log", admin_views.log_page, name="admin_log"),
    path("admin/incidents", admin_views.incidents_page, name="admin_incidents"),
    path("admin/incidents/<int:pk>", admin_views.incident_page, name="admin_incident"),
    path("admin/incidents/<int:pk>/updates", admin_views.incident_update, name="admin_incident_update"),
    path("api/admin/users", admin_views.api_users, name="api_admin_users"),
    path("api/admin/accounts", admin_views.api_accounts, name="api_admin_accounts"),
    path("api/admin/requests", admin_views.api_requests, name="api_admin_requests"),
    path("api/admin/requests/<rid:request_id>", admin_views.api_request, name="api_admin_request"),
]
