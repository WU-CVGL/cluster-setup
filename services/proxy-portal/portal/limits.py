"""Failed sign-ins per client address and per username, in the database so every worker shares them."""

import datetime

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import SignInFailure


def client_address(request):
    return (request.META.get(settings.CLIENT_IP_HEADER) or request.META.get("REMOTE_ADDR") or "")[:64]


def _keys(request, username):
    return [("client", client_address(request)), ("username", username.strip().lower()[:256])]


def _since():
    return timezone.now() - datetime.timedelta(seconds=settings.SIGNIN_WINDOW)


def blocked(request, username):
    """True while the client or the username has reached its limit of failures in the window."""
    since = _since()
    SignInFailure.objects.filter(at__lt=since).delete()
    limits = {"client": settings.SIGNIN_MAX_PER_CLIENT, "username": settings.SIGNIN_MAX_PER_USERNAME}
    for scope, key in _keys(request, username):
        if SignInFailure.objects.filter(scope=scope, key=key, at__gte=since).count() >= limits[scope]:
            return True
    return False


def record_failure(request, username):
    now = timezone.now()
    with transaction.atomic():
        SignInFailure.objects.bulk_create([SignInFailure(scope=scope, key=key, at=now)
                                           for scope, key in _keys(request, username)])
