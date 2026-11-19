"""
The admin console: proxy accounts, account requests with their outcome, the change log and incidents. Admins are
the users whose Determined admin flag was true at sign-in (the session's is_determined_admin); every view here checks
it. Pages send anonymous users to sign-in, the APIs answer 401; a signed-in non-admin gets 403.

Account changes are requests: the portal writes them into the applier's spool and shows the applier's outcome. It
never changes the accounts file and never keeps a password.
"""

import functools
import json
import logging
import urllib.parse

from django import forms
from django.conf import settings
from django.http import Http404, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import accounts, lifecycle, proxy_status
from .models import AccountRequest, ClusterUser, Incident, IncidentUpdate
from .templatetags.portal import names
from .views import SESSION_ADMIN, _signin_redirect, session_user

log = logging.getLogger("portal.admin")

LOG_LIMIT = 200


def admin_only(kind):
    """
    kind "page": anonymous users go to sign-in; "api": 401. Non-admins get 403 either way, with nothing done; then
    the CSRF check (403) for every post.
    """

    def decorator(view):
        protected = csrf_protect(view)

        @functools.wraps(view)
        def wrapped(request, *args, **kwargs):
            if session_user(request) is None:
                if kind == "api":
                    return JsonResponse({"error": "sign-in required"}, status=401)
                return _signin_redirect(request)
            if request.session.get(SESSION_ADMIN) is not True:
                if kind == "api":
                    return JsonResponse({"error": "Determined admins only"}, status=403)
                return render(request, "portal/admin/forbidden.html", {"nav": "admin"}, status=403)
            return protected(request, *args, **kwargs)  # the CSRF check, for admins only

        wrapped.admin_only = kind
        # the sign-in and admin checks come first, so an anonymous post gets 401 or the sign-in redirect
        return csrf_exempt(never_cache(wrapped))

    return decorator


# ------------------------------------------------------------------ accounts


_restart_text = accounts.restart_text


def _effect_text(op, name, proxy=None, row=None):
    # a grant or revoke changes this account's access to one private proxy: say just that, with what stays
    if op == "grant":
        return _("Gives %(name)s the private proxy %(proxy)s, to use at its own address. The private proxies it was "
                 "given before stay as they are, and the Private Auto-switching Proxy stays for admin accounts "
                 "only.") % {"name": name, "proxy": proxy}
    if op == "revoke" and row is not None and row.admin:
        return _("Takes the private proxy %(proxy)s back from %(name)s. As an admin account, it still reaches "
                 "%(proxy)s and every other private proxy.") % {"name": name, "proxy": proxy}
    if op == "revoke":
        return _("Takes the private proxy %(proxy)s back from %(name)s: %(proxy)s stops accepting it. The other "
                 "private proxies it was given stay as they are.") % {"name": name, "proxy": proxy}
    if op == "admin-off" and row is not None and row.proxies:
        return _("%(name)s stops being an admin account: the Private Auto-switching Proxy and the private proxies "
                 "refuse it, except %(proxies)s, which it was given.") % {"name": name, "proxies": names(row.proxies)}
    return {
        "add": _("Creates the proxy account %(name)s with a new random password, which you see once when the change "
                 "is live. It may use the Auto-switching Proxy and the public proxies."),
        "rotate": _("Gives %(name)s a new random password, which you see once when it is live. The old password "
                    "stops working."),
        "enable": _("Turns proxy access on for %(name)s, with its current password."),
        "disable": _("Turns proxy access off for %(name)s. The account and its password are kept: turning it on "
                     "again restores it."),
        "remove": _("Removes the account %(name)s and its password. Adding it again gives it a new password."),
        "admin-on": _("Makes %(name)s an admin account: it may also use the Private Auto-switching Proxy and the "
                      "private proxies. This is the proxy account's flag, not the Determined admin role."),
        "admin-off": _("%(name)s stops being an admin account: the Private Auto-switching Proxy and the private "
                       "proxies refuse it."),
    }[op] % {"name": name}


def raw_auth_text(status):
    if status.raw_auth and status.open_proxies:
        return _("The public proxies' own addresses require an account, except those of %(open)s, which need none: "
                 "an account whose proxy access is off is refused everywhere else.") % {
            "open": names(status.open_proxies)}
    if status.raw_auth:
        return _("The public proxies' own addresses require an account: an account whose proxy access is off is "
                 "refused at every proxy.")
    text = _("The public proxies' own addresses need no account: proxy access off stops only the auto-switching "
             "proxies and the private proxies, and a public proxy's own address still works for that user.")
    if status.open_proxies:  # what stays so once the public proxies' own addresses require an account
        text = lifecycle.sentences(text, ngettext(
            "%(open)s needs no account at its own address even when the public proxies require one.",
            "%(open)s need no account at their own addresses even when the public proxies require one.",
            len(status.open_proxies)) % {"open": names(status.open_proxies)})
    return text


def _pending_by_account():
    rows = lifecycle.refresh_all(AccountRequest.objects.exclude(state__in=AccountRequest.FINAL)[:200])
    pending = {}
    for row in rows:
        if not row.final and row.username not in pending:
            pending[row.username] = row
    return pending


def _account_rows(status, query):
    pending = _pending_by_account()
    rows = []
    for row in status.all_accounts:
        if query and query not in row.name:
            continue
        rows.append({"name": row.name, "kind": row.kind, "enabled": row.enabled, "admin": row.admin,
                     "protected": row.protected, "proxies": list(row.proxies),
                     "actions": accounts.actions(row, status), "pending": pending.get(row.name)})
    return rows


@admin_only("page")
@require_GET
def accounts_page(request):
    """The accounts list is part of the Users page now."""
    query = (request.GET.get("q") or "").strip()[:64]
    return redirect(reverse("admin_users") + (f"?q={urllib.parse.quote(query)}" if query else ""))


# ------------------------------------------------------------------ users

USER_FILTERS = ("no-account", "disabled", "private", "inactive")


def _user_filter_match(flt, row):
    if flt == "no-account":
        return row["account"] == "none"
    if flt == "disabled":
        return row["account"] == "disabled"
    if flt == "private":
        return row["private"]
    if flt == "inactive":  # deactivated in Determined, proxy access still on
        return row["active"] is False and row["account"] == "enabled"
    return True


def users_view(status, query="", flt=""):
    """
    Every cluster user of the list read at the last admin sign-in, and every user account in the accounts status
    (also one whose name is not in that list), with their Determined flags, proxy account and the actions; the
    service accounts apart. query matches the name; flt is one of USER_FILTERS.
    """
    cluster = {u.username: u for u in ClusterUser.objects.all()}
    captured_at = max((u.captured_at for u in cluster.values()), default=None)
    user_accounts = {r.name: r for r in status.all_accounts if r.kind == "user"} if status.available else {}
    pending = _pending_by_account()
    rows, counts = [], dict.fromkeys(USER_FILTERS, 0)
    for name in sorted(set(cluster) | set(user_accounts)):
        user, account = cluster.get(name), user_accounts.get(name)
        if account is not None:
            actions = accounts.actions(account, status)
        elif status.available and accounts.valid_name(name) and accounts.kind(name) == "user":
            actions = ["add"]
        else:
            actions = []
        row = {"name": name, "listed": user is not None, "active": user.active if user else None,
               "determined_admin": user.admin if user else None,
               "account": None if not status.available else "none" if account is None
               else "enabled" if account.enabled else "disabled",
               "private": bool(account and account.admin), "protected": bool(account and account.protected),
               "proxies": list(account.proxies) if account else [],
               "valid_name": accounts.valid_name(name), "actions": actions, "pending": pending.get(name)}
        for key in USER_FILTERS:
            counts[key] += _user_filter_match(key, row)
        if query and query not in name.lower():
            continue
        if flt and not _user_filter_match(flt, row):
            continue
        rows.append(row)
    services = [r for r in _account_rows(status, query) if r["kind"] == "service"] if status.available else []
    return {"users": rows, "services": services, "counts": counts, "captured_at": captured_at,
            "listed": len(cluster)}


def _user_params(params):
    query = (params.get("q") or "").strip().lower()[:64]
    flt = params.get("filter") or ""
    return query, flt if flt in USER_FILTERS else ""


@admin_only("page")
@require_GET
def users_page(request):
    status = proxy_status.load()
    query, flt = _user_params(request.GET)
    view = users_view(status, query, flt)
    return render(request, "portal/admin/users.html", dict(view, **{
        "nav": "admin", "subnav": "users", "status": status, "query": query, "filter": flt,
        "filters": [(key, view["counts"][key]) for key in USER_FILTERS],
        "raw_auth_text": raw_auth_text(status), "suggestions": accounts.suggestions(status, settings.NODES),
        "recent": lifecycle.refresh_all(AccountRequest.objects.all()[:8]),
    }))


def _create(request, op, username, proxy=None):
    """Check, record and write one request. Raises accounts.Refused before anything is written."""
    status = proxy_status.load()
    accounts.check(op, username, status, proxy)
    return accounts.submit(op, username, session_user(request), proxy=proxy)


@admin_only("page")
@require_http_methods(["GET", "POST"])
def confirm_page(request, op):
    if op not in accounts.OPS:
        raise Http404()
    source = request.POST if request.method == "POST" else request.GET
    username = (source.get("username") or "").strip()[:128]
    proxy = None
    if op in accounts.GRANT_OPS:  # no other op names a proxy
        proxy = (source.get("proxy") or "").strip()[:200] or None
    status = proxy_status.load()
    row = next((r for r in status.all_accounts if r.name == username), None)
    context = {"nav": "admin", "subnav": "users", "op": op, "username": username, "row": row, "status": status,
               "destructive": op in accounts.DESTRUCTIVE, "error": None, "proxy": proxy, "choices": None}
    try:
        if op in accounts.GRANT_OPS and proxy is None and request.method == "GET":
            # first the proxy: a grant offers the private proxies the account may not use yet, a revoke those given
            accounts.check_account(op, username, status)
            context["choices"] = accounts.proxy_choices(op, row, status)
            if not context["choices"]:
                raise accounts.Refused(_("There is no private proxy to choose for %(name)s.") % {"name": username},
                                       409)
            return render(request, "portal/admin/confirm.html", context)
        accounts.check(op, username, status, proxy)
    except accounts.Refused as e:
        context["error"] = str(e)
        return render(request, "portal/admin/confirm.html", context, status=e.status)
    context.update(effect=_effect_text(op, username, proxy, row), restart=_restart_text(op, row, status, proxy=proxy),
                   raw_auth=raw_auth_text(status) if op in ("disable", "enable", "remove") else None)
    if request.method == "POST":
        if op in accounts.DESTRUCTIVE and request.POST.get("confirm", "").strip() != username:
            context["error"] = _("Type the account name to confirm.")
            return render(request, "portal/admin/confirm.html", context, status=400)
        try:
            created = _create(request, op, username, proxy)
        except accounts.Refused as e:
            context["error"] = str(e)
            return render(request, "portal/admin/confirm.html", context, status=e.status)
        return redirect("admin_request", request_id=created.id)
    return render(request, "portal/admin/confirm.html", context)


@admin_only("page")
@require_GET
def request_page(request, request_id):
    row = lifecycle.refresh(get_object_or_404(AccountRequest, id=request_id))
    pw = lifecycle.password_state(row, session_user(request))
    status = proxy_status.load()
    return render(request, "portal/admin/request.html", {
        "nav": "admin", "subnav": "log", "row": row, "level": lifecycle.level(row), "text": lifecycle.describe(row),
        "password": pw, "password_text": lifecycle.password_text(pw, row) if pw else None,
        "raw_auth": raw_auth_text(status) if row.op in ACCESS_OPS and status.available else None,
    })


@admin_only("page")
@require_POST
@sensitive_variables("password")
def password_page(request, request_id):
    """Show the one-time password to the admin who made the request: claim, read, delete, render. Never kept."""
    username = session_user(request)
    row = lifecycle.refresh(get_object_or_404(AccountRequest, id=request_id))
    if lifecycle.password_state(row, username) == "other":
        return render(request, "portal/admin/forbidden.html", {"nav": "admin"}, status=403)
    state, password = lifecycle.collect(row, username)
    response = render(request, "portal/admin/password.html", {
        "nav": "admin", "subnav": "log", "row": row, "password_value": password,
        "password_text": lifecycle.password_text(state, row) if password is None else None})
    password = None
    return response


def _request_json(row, user, status):
    data = lifecycle.as_json(row, user)
    # proxy access on or off: say where it applies
    data["raw_auth_text"] = raw_auth_text(status) if row.op in ACCESS_OPS and status.available else None
    return data


ACCESS_OPS = ("disable", "enable", "remove")

# ------------------------------------------------------------------ change log


FILTERS = ("username", "requested_by", "op", "state")


def _filtered(params):
    rows = AccountRequest.objects.all()
    applied = {}
    for key in FILTERS:
        value = (params.get(key) or "").strip()[:128]
        if value:
            applied[key] = value
            rows = rows.filter(**({f"{key}__icontains": value} if key in ("username", "requested_by")
                                  else {key: value}))
    return lifecycle.refresh_all(rows[:LOG_LIMIT]), applied


@admin_only("page")
@require_GET
def log_page(request):
    # outcomes are copied from the status files first, so a state filter sees them
    lifecycle.refresh_all(AccountRequest.objects.exclude(state__in=AccountRequest.FINAL)[:LOG_LIMIT])
    rows, applied = _filtered(request.GET)
    status = proxy_status.load()
    return render(request, "portal/admin/log.html", {
        "nav": "admin", "subnav": "log", "rows": [(row, lifecycle.level(row), lifecycle.describe(row))
                                                  for row in rows],
        "raw_auth_text": raw_auth_text(status) if status.available else None,
        "filters": applied, "ops": AccountRequest.OPS, "states": AccountRequest.STATES, "limit": LOG_LIMIT})


# ------------------------------------------------------------------ APIs


@admin_only("api")
def api_accounts(request):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    status = proxy_status.load()
    query = (request.GET.get("q") or "").strip().lower()[:64]
    rows = _account_rows(status, query) if status.available else []
    for row in rows:
        pending = row.pop("pending")
        row["pending"] = {"id": pending.id, "op": pending.op, "state": pending.state} if pending else None
    return JsonResponse({
        "status_available": status.available,
        "status_stale": status.stale if status.available else None,
        "status_generated_at": status.generated_at.isoformat() if status.generated_at else None,
        "raw_auth": status.raw_auth if status.available else None,
        "raw_auth_text": raw_auth_text(status) if status.available else None,
        "status_unapplied_changes": status.unapplied_changes if status.available else None,
        "accounts": rows,
    })


@admin_only("api")
def api_users(request):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    status = proxy_status.load()
    query, flt = _user_params(request.GET)
    view = users_view(status, query, flt)
    for row in view["users"] + view["services"]:
        pending = row.pop("pending")
        row["pending"] = {"id": pending.id, "op": pending.op, "state": pending.state} if pending else None
    return JsonResponse({
        "users_captured_at": view["captured_at"].isoformat() if view["captured_at"] else None,
        "status_available": status.available,
        "status_stale": status.stale if status.available else None,
        "raw_auth": status.raw_auth if status.available else None,
        "counts": view["counts"], "users": view["users"], "service_accounts": view["services"],
    })


@admin_only("api")
def api_requests(request):
    """GET: the change log, filtered. POST {"op", "username", "confirm"}: a new request (202)."""
    if request.method == "GET":
        rows, _applied = _filtered(request.GET)
        user, status = session_user(request), proxy_status.load()
        return JsonResponse({"requests": [_request_json(row, user, status) for row in rows]})
    if request.method != "POST":
        return HttpResponseNotAllowed(["GET", "POST"])
    try:
        body = json.loads(request.body.decode("utf-8")) if request.content_type == "application/json" \
            else {k: v for k, v in request.POST.items() if k != "csrfmiddlewaretoken"}
    except (UnicodeDecodeError, ValueError):
        return JsonResponse({"error": "not UTF-8 JSON"}, status=400)
    if not isinstance(body, dict) or not set(body) <= {"op", "username", "confirm", "proxy"} \
            or not all(isinstance(v, str) for v in body.values()) \
            or ("proxy" in body and body.get("op") not in accounts.GRANT_OPS):
        return JsonResponse({"error": "expected an object with op, username, for grant and revoke proxy (a private "
                                      "proxy) and, for rotate, disable, remove, admin-off and revoke, confirm (the "
                                      "account name)"}, status=400)
    op, username, proxy = body.get("op", ""), body.get("username", ""), body.get("proxy")
    if op in accounts.DESTRUCTIVE and body.get("confirm") != username:
        return JsonResponse({"error": "confirm must equal the account name for this operation"}, status=400)
    try:
        row = _create(request, op, username, proxy)
    except accounts.Refused as e:
        return JsonResponse({"error": str(e)}, status=e.status)
    return JsonResponse(_request_json(row, session_user(request), proxy_status.load()), status=202)


@admin_only("api")
def api_request(request, request_id):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    row = AccountRequest.objects.filter(id=request_id).first()
    if row is None:
        return JsonResponse({"error": "no such request"}, status=404)
    return JsonResponse(_request_json(lifecycle.refresh(row), session_user(request), proxy_status.load()))


# ------------------------------------------------------------------ incidents


class IncidentForm(forms.ModelForm):
    started_at = forms.DateTimeField(required=False, widget=forms.DateTimeInput(
        attrs={"class": "input", "type": "datetime-local"}, format="%Y-%m-%dT%H:%M"))

    class Meta:
        model = Incident
        fields = ["title", "component", "state", "body", "started_at"]
        widgets = {"title": forms.TextInput(attrs={"class": "input", "maxlength": 200}),
                   "component": forms.Select(attrs={"class": "input"}),
                   "state": forms.Select(attrs={"class": "input"}),
                   "body": forms.Textarea(attrs={"class": "input", "rows": 4})}


class UpdateForm(forms.ModelForm):
    class Meta:
        model = IncidentUpdate
        fields = ["state", "body"]
        widgets = {"state": forms.Select(attrs={"class": "input"}),
                   "body": forms.Textarea(attrs={"class": "input", "rows": 4, "maxlength": 5000})}


@admin_only("page")
@require_http_methods(["GET", "POST"])
def incidents_page(request):
    form = IncidentForm(request.POST or None, initial={"state": "investigating"})
    if request.method == "POST" and form.is_valid():
        incident = form.save(commit=False)
        incident.started_at = form.cleaned_data["started_at"] or timezone.now()
        incident.author = session_user(request)
        incident.set_state(incident.state)
        incident.save()
        log.info("incident %s created by %s", incident.id, incident.author)
        return redirect("admin_incident", pk=incident.id)
    return render(request, "portal/admin/incidents.html", {
        "nav": "admin", "subnav": "incidents", "form": form,
        "incidents": Incident.objects.prefetch_related("updates").all()[:100]},
        status=400 if request.method == "POST" else 200)


@admin_only("page")
@require_http_methods(["GET", "POST"])
def incident_page(request, pk):
    incident = get_object_or_404(Incident, pk=pk)
    started_at = incident.started_at  # validating the form writes the cleared field into incident
    form = IncidentForm(request.POST or None, instance=incident)
    update = UpdateForm(initial={"state": incident.state})
    if request.method == "POST" and form.is_valid():
        edited = form.save(commit=False)
        edited.started_at = form.cleaned_data["started_at"] or started_at
        edited.set_state(edited.state)
        edited.save()
        log.info("incident %s edited by %s", incident.id, session_user(request))
        return redirect("admin_incident", pk=incident.id)
    return render(request, "portal/admin/incident.html", {
        "nav": "admin", "subnav": "incidents", "incident": incident, "form": form, "update": update},
        status=400 if request.method == "POST" else 200)


@admin_only("page")
@require_POST
def incident_update(request, pk):
    incident = get_object_or_404(Incident, pk=pk)
    update = UpdateForm(request.POST)
    if not update.is_valid():
        return render(request, "portal/admin/incident.html", {
            "nav": "admin", "subnav": "incidents", "incident": incident, "form": IncidentForm(instance=incident),
            "update": update}, status=400)
    item = update.save(commit=False)
    item.incident, item.author = incident, session_user(request)
    item.save()
    incident.set_state(item.state, at=item.posted_at)
    incident.save()
    log.info("incident %s updated by %s", incident.id, item.author)
    return redirect(reverse("admin_incident", kwargs={"pk": incident.id}))
