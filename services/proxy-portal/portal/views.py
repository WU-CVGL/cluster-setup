import datetime
import logging
import time

from django.conf import settings
from django.db import DatabaseError, transaction
from django.http import HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import dateformat, timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.utils.translation import gettext_noop
from django.utils.translation import ngettext
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_GET, require_POST

from . import access, charts, health, limits, master, usage
from .models import ClusterUser, Incident
from .templatetags.portal import percent

log = logging.getLogger("portal.signin")

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
    shown = Incident.objects.prefetch_related("updates")
    open_ = list(shown.exclude(state="resolved")[:20])
    closed = list(shown.filter(state="resolved", resolved_at__gte=since)[:20])
    return open_ + closed


def _local(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).astimezone(timezone.get_current_timezone())


STATE_TEXTS = {"ok": gettext_noop("Passing"), "degraded": gettext_noop("Degraded"), "bad": gettext_noop("Failing"),
               "unknown": gettext_noop("Unknown")}


def _bar_items(bars, name):
    """One row's buckets for the page, oldest first, each with its period in local time and its label."""
    row, step = bars["rows"][name], bars["step_seconds"]
    start = bars["end"] - (bars["count"] - 1) * step
    texts = {key: _(value) for key, value in STATE_TEXTS.items()}
    items = []
    for i, state in enumerate(row["state"]):
        end = _local(start + i * step)
        begin = _local(start + i * step - step)
        when = (f"{dateformat.format(begin, 'Y-m-d H:i')}–{dateformat.format(end, 'H:i')}"
                if begin.date() == end.date() else
                f"{dateformat.format(begin, 'Y-m-d H:i')} – {dateformat.format(end, 'Y-m-d H:i')}")
        rate, coverage = row["success_rate"][i], row["coverage"][i]
        detail = (_("no probe results") if rate is None else
                  _("success %(rate)s, coverage %(coverage)s") % {"rate": percent(rate, 2),
                                                                   "coverage": percent(coverage, 0)})
        items.append({"state": state, "label": f"{when} · {texts[state]} · {detail}"})
    return items


def _status_context():
    data = health.build()
    names = list(data["entries"]) + [proxy["name"] for proxy in data["proxies"]]
    data["bar_items"] = {name: {window: _bar_items(bars, name) for window, bars in data["bars"].items()}
                         for name in names}
    return data


def _live(data):
    """What the status page's script refreshes, as /api/status/live returns it: it polls that and ages the results."""
    return {key: data[key] for key in ("monitoring_available", "headline", "last_check_age_seconds",
                                       "stale_after_seconds", "entries", "proxies")}


def _live_texts():
    """The page script's texts, in the page's language."""
    return {
        "passing": _("Passing"), "failing": _("Failing"), "unknown": _("Unknown"),
        "s_ago": _("%(n)d s ago"), "min_ago": _("%(n)d min ago"), "h_ago": _("%(n)d h ago"),
        "days_ago": _("%(n)d days ago"),
        "headline_unknown": _("Status unknown: no recent probe results for the Auto-switching Proxy"),
        "entry_count": _("%(passing)s of %(configured)s proxies passing"),
    }


@require_GET
def status_page(request):
    data = _status_context()
    return render(request, "portal/status.html", {"nav": "status", "data": data, "incidents": incidents(),
                                                  "live": _live(data), "live_texts": _live_texts()})


@require_GET
def api_status(request):
    data = health.build()
    data["incidents"] = [incident.as_json() for incident in incidents()]
    return JsonResponse(data)


@require_GET
def api_status_live(request):
    """What the Status page polls: /api/status without the bars and the incidents, which update on reload."""
    return JsonResponse(_live(health.build(bars=False)))


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
    from . import self_reset  # it uses this module's session helpers

    view = access.build(username)
    return render(request, "portal/access.html", {"nav": "access", "view": view,
                                                  "password_placeholder": "<password>",
                                                  "reset": self_reset.page_context(username),
                                                  "reset_texts": {
                                                      "failed": _("The reset could not be started. Nothing "
                                                                  "changed."),
                                                      "state": _("The reset's state could not be read. Reload the "
                                                                 "page to see it."),
                                                      "network": _("The portal could not be reached. Try again."),
                                                      "collecting": _("Getting the password…")}})


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
    if result.users is not None:
        try:
            store_users(result.users)
        except DatabaseError as e:  # the sign-in goes on with the previous list
            log.warning("cluster user list not stored: %s", type(e).__name__)
    request.session.flush()
    request.session.cycle_key()
    request.session[SESSION_USER] = result.username
    request.session[SESSION_ADMIN] = result.is_admin
    request.session[SESSION_AT] = int(time.time())
    return redirect(_next_url(request, next_url))


def store_users(users):
    """Replace the cluster user list with the one read at an admin's sign-in."""
    now = timezone.now()
    with transaction.atomic():
        ClusterUser.objects.all().delete()
        ClusterUser.objects.bulk_create([ClusterUser(username=name, active=active, admin=admin, captured_at=now)
                                         for name, active, admin in users])


@require_POST
@csrf_protect
def signout(request):
    request.session.flush()
    return redirect(reverse("status"))


def healthz(request):
    return HttpResponse("ok\n", content_type="text/plain")
