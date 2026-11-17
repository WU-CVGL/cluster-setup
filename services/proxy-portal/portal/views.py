import datetime
import time

from django.conf import settings
from django.http import HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_GET, require_POST

from . import access, charts, health, limits, master, usage
from .models import Incident

SESSION_USER = "username"
SESSION_ADMIN = "is_determined_admin"
SESSION_AT = "signed_in_at"


def session_user(request):
    """The signed-in Determined username, from the session only."""
    username = request.session.get(SESSION_USER)
    return username if isinstance(username, str) and username else None


def portal_context(request):
    return {
        "signed_in_user": session_user(request),
        "is_determined_admin": request.session.get(SESSION_ADMIN) is True,
        "csp_nonce": getattr(request, "csp_nonce", ""),
        "session_hours": round(settings.SESSION_COOKIE_AGE / 3600),
    }


def incidents():
    since = timezone.now() - datetime.timedelta(days=30)
    open_ = list(Incident.objects.exclude(state="resolved")[:20])
    closed = list(Incident.objects.filter(state="resolved", resolved_at__gte=since)[:20])
    return open_ + closed


def _local(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).astimezone(timezone.get_current_timezone())


def _status_context():
    data = health.build()
    for day in data["history"]["days"]:
        day["end_local"] = _local(day["end"])
        day["start_local"] = _local(day["end"] - 86400)
    return data


@require_GET
def status_page(request):
    data = _status_context()
    return render(request, "portal/status.html", {"nav": "status", "data": data, "incidents": incidents()})


@require_GET
def api_status(request):
    data = health.build()
    data["incidents"] = [incident.as_json() for incident in incidents()]
    return JsonResponse(data)


def _refuse_parameters(request):
    """/api/me/* takes nothing from the client: the user is the session's."""
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    if session_user(request) is None:
        return JsonResponse({"error": "sign-in required"}, status=401)
    if request.GET:
        return JsonResponse({"error": "this endpoint takes no parameters"}, status=400)
    return None


@never_cache
def api_me_access(request):
    refused = _refuse_parameters(request)
    if refused is not None:
        return refused
    return JsonResponse(access.build(session_user(request)))


@never_cache
def api_me_usage(request):
    refused = _refuse_parameters(request)
    if refused is not None:
        return refused
    return JsonResponse(usage.build(session_user(request)))


def _signin_redirect(request):
    return redirect(reverse("signin") + "?next=" + request.path)


@require_GET
def access_page(request):
    username = session_user(request)
    if username is None:
        return _signin_redirect(request)
    view = access.build(username)
    return render(request, "portal/access.html", {"nav": "access", "view": view,
                                                  "password_placeholder": "<password>"})


@require_GET
def usage_page(request):
    username = session_user(request)
    if username is None:
        return _signin_redirect(request)
    view = usage.build(username)
    chart_data = {window: charts.bars(trend["points"], trend["step_seconds"], _local)
                  for window, trend in view["trend"].items()}
    return render(request, "portal/usage.html", {"nav": "usage", "view": view, "charts": chart_data,
                                                 "windows": ["24h", "7d", "30d"]})


def _next_url(request, value):
    if value and url_has_allowed_host_and_scheme(value, allowed_hosts={request.get_host()},
                                                 require_https=request.is_secure()):
        return value
    return reverse("access")


@sensitive_post_parameters("password")
@sensitive_variables("password")
@csrf_protect
@never_cache
def signin(request):
    next_url = request.POST.get("next") or request.GET.get("next") or ""
    context = {"nav": "signin", "next": next_url, "error": None, "username": ""}
    if request.method == "GET":
        return render(request, "portal/signin.html", context)
    if request.method != "POST":
        return HttpResponseNotAllowed(["GET", "POST"])
    username = (request.POST.get("username") or "").strip()[:128]
    password = request.POST.get("password") or ""
    context["username"] = username
    if not username or not password:
        context["error"] = _("Enter your Determined username and password.")
        return render(request, "portal/signin.html", context, status=400)
    if limits.blocked(request, username):
        minutes = -(-settings.SIGNIN_WINDOW // 60)
        context["error"] = ngettext("Too many failed sign-ins. Try again in %(n)d minute.",
                                    "Too many failed sign-ins. Try again in %(n)d minutes.", minutes) % {"n": minutes}
        return render(request, "portal/signin.html", context, status=429)
    result = master.check_credentials(username, password)
    password = None
    if result.result == master.UNAVAILABLE:
        context["error"] = _("The Determined master cannot check your sign-in right now. Try again later.")
        return render(request, "portal/signin.html", context, status=503)
    if result.result != master.OK:
        limits.record_failure(request, username)
        context["error"] = (_("This Determined account is deactivated.") if result.result == master.INACTIVE
                            else _("Wrong username or password."))
        return render(request, "portal/signin.html", context, status=401 if result.result == master.INVALID
                      else 403)
    request.session.flush()
    request.session.cycle_key()
    request.session[SESSION_USER] = result.username
    request.session[SESSION_ADMIN] = result.is_admin
    request.session[SESSION_AT] = int(time.time())
    return redirect(_next_url(request, next_url))


@require_POST
@csrf_protect
def signout(request):
    request.session.flush()
    return redirect(reverse("status"))


def healthz(request):
    return HttpResponse("ok\n", content_type="text/plain")
