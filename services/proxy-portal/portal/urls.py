from django.urls import path
from django.views.i18n import set_language

from . import views

urlpatterns = [
    path("", views.status_page, name="status"),
    path("access", views.access_page, name="access"),
    path("usage", views.usage_page, name="usage"),
    path("signin", views.signin, name="signin"),
    path("signout", views.signout, name="signout"),
    path("language", set_language, name="set_language"),
    path("api/status", views.api_status, name="api_status"),
    path("api/me/access", views.api_me_access, name="api_me_access"),
    path("api/me/usage", views.api_me_usage, name="api_me_usage"),
    path("healthz", views.healthz, name="healthz"),
]
