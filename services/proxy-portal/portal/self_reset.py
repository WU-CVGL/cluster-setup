"""
A user's reset of their own proxy password, from My access: a rotate request for the session's own account, with
requested_by that user and self_service set, through the same spool as the admins' requests. Only that user may
follow it and collect the new password, once (lifecycle.collect); admins see the request in the change log but
never its password. Refused for service accounts, users without an account and disabled accounts, while a request
for the account is pending, and beyond SELF_RESET_MAX_PER_HOUR requests per user.
"""

import datetime
import functools
import json
import threading

from django.conf import settings
from django.http import HttpResponseNotAllowed, JsonResponse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.debug import sensitive_variables

from . import accounts, lifecycle, proxy_status
from .models import AccountRequest
from .views import session_user

WINDOW = datetime.timedelta(hours=1)
# the checks and the new request in one step: one process serves the portal (Dockerfile), with threads
_SUBMIT = threading.Lock()


def _recent(username):
    return AccountRequest.objects.filter(requested_by=username, username=username, self_service=True,
                                         requested_at__gte=timezone.now() - WINDOW)


def _pending(username):
    """The account's requests still waiting or applying, by anyone, with their outcome read first."""
    rows = lifecycle.refresh_all(AccountRequest.objects.filter(username=username)
                                 .exclude(state__in=AccountRequest.FINAL)[:20])
    return [row for row in rows if not row.final]


def refusal(username, status):
    """None when username may reset their password now, else (code, message, HTTP status)."""
    if not status.available:
        return "unavailable", _("Account information is unavailable right now. Try again later."), 503
    account = status.accounts.get(username)
    if account is None or username.startswith("svc-") or username in proxy_status.PROTECTED:
        return "none", _("There is no proxy account named after your Determined username."), 404
    if not account.enabled:
        return "disabled", _("Password reset is unavailable while proxy access is off for your account. Ask an "
                             "admin to turn it on."), 409
    if _pending(username):
        return "pending", _("A change to your account is still waiting or being applied. Try again once it is "
                            "done."), 409
    limit = settings.SELF_RESET_MAX_PER_HOUR
    if _recent(username).count() >= limit:
        return "limit", ngettext("You can reset your password %(n)d time per hour. Try again later.",
                                 "You can reset your password %(n)d times per hour. Try again later.",
                                 limit) % {"n": limit}, 429
    return None


def _current(username):
    """The user's latest self-service reset that is still running or holds a password to collect, else None."""
    row = AccountRequest.objects.filter(requested_by=username, username=username, self_service=True) \
        .order_by("-requested_at").first()
    if row is None:
        return None
    lifecycle.refresh(row)
    return row if not row.final or lifecycle.password_state(row, username) in ("waiting", "ready") else None


def as_json(row, username):
    """What the requesting user sees of their reset: no stage, error or restarted services."""
    pw = lifecycle.password_state(row, username)
    return {"id": row.id, "state": row.state, "state_label": row.get_state_display(), "final": row.final,
            "level": lifecycle.level(row), "text": lifecycle.describe(row),
            "requested_at": row.requested_at.isoformat(), "password": pw,
            "password_text": lifecycle.password_text(pw, row) if pw else None}


def page_context(username):
    """For My access: whether the button shows, why it is unavailable, and a reset to resume."""
    status = proxy_status.load()
    refused = refusal(username, status)
    code = refused[0] if refused else None
    current = _current(username)
    account = status.accounts.get(username) if status.available else None
    return {
        "shown": code not in ("unavailable", "none"),
        "available": code is None,
        # a reset of the user's own still running is resumed, not explained
        "reason": refused[1] if refused and not (code == "pending" and current) else None,
        "current": as_json(current, username) if current else None,
        "restart": accounts.restart_text("rotate", account, status) if account else "",
    }


# ------------------------------------------------------------------ the API


def signed_in_api(view):
    """401 without a session, before the CSRF check (403) of a post; never cached."""
    protected = csrf_protect(view)

    @functools.wraps(view)
    def wrapped(request, *args, **kwargs):
        if session_user(request) is None:
            return JsonResponse({"error": "sign-in required"}, status=401)
        return protected(request, *args, **kwargs)

    return csrf_exempt(never_cache(wrapped))


def _takes_nothing(request):
    """These posts take nothing from the client but the CSRF token: the account is the session's."""
    if request.GET or set(request.POST) - {"csrfmiddlewaretoken"}:
        return False
    if request.content_type == "application/json":
        try:
            body = json.loads(request.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            return False
        return body == {}
    return True


@signed_in_api
def api_reset(request):
    """POST: a new password for the session's own proxy account (202), shown once to this user when live."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not _takes_nothing(request):
        return JsonResponse({"error": "this endpoint takes no parameters: it resets your own account"}, status=400)
    username = session_user(request)
    status = proxy_status.load()
    with _SUBMIT:
        refused = refusal(username, status)
        if refused:
            return JsonResponse({"error": refused[1], "reason": refused[0]}, status=refused[2])
        try:
            accounts.check("rotate", username, status)
            row = accounts.submit("rotate", username, username, self_service=True)
        except accounts.Refused as e:
            return JsonResponse({"error": str(e)}, status=e.status)
    return JsonResponse(as_json(row, username), status=202)


def _own(request, request_id):
    """The session user's own self-service request, or an error response: 404 unknown, 403 another user's."""
    row = AccountRequest.objects.filter(id=request_id).first()
    if row is None:
        return None, JsonResponse({"error": "no such request"}, status=404)
    if row.requested_by != session_user(request):
        return None, JsonResponse({"error": "only the user who requested it can see it"}, status=403)
    if not row.self_service:
        return None, JsonResponse({"error": "not a password reset from My access"}, status=404)
    return lifecycle.refresh(row), None


@signed_in_api
def api_reset_state(request, request_id):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    if request.GET:
        return JsonResponse({"error": "this endpoint takes no parameters"}, status=400)
    row, refused = _own(request, request_id)
    return refused or JsonResponse(as_json(row, session_user(request)))


@signed_in_api
@sensitive_variables("password", "data")
def api_reset_password(request, request_id):
    """POST: the new password, once: read from the applier's result and deleted at once; never kept."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not _takes_nothing(request):
        return JsonResponse({"error": "this endpoint takes no parameters"}, status=400)
    row, refused = _own(request, request_id)
    if refused:
        return refused
    username = session_user(request)
    state, password = lifecycle.collect(row, username)
    data = as_json(row, username)
    data.update(password=state, password_text=None if password else lifecycle.password_text(state, row),
                value=password)
    response = JsonResponse(data, status=200 if password else 409)
    password = data = None
    return response
