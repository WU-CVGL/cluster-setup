"""
The admins' account operations: which requests the portal may write, checked before any file is written. The
applier checks every request again (applier/proxy_apply.py); the accounts file stays the only record of accounts.
"""

import logging
import re

from django.utils.translation import gettext as _

from . import spool
from .proxy_status import MAX_PROXY_NAME, PROTECTED, PROXY_NAME_RE
from .templatetags.portal import names

log = logging.getLogger("portal.requests")

# proxy_accounts.py's validate_name, which the image does not contain; tests/test_admin_requests.py compares the two
NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
SERVICE_RE = re.compile(r"svc-(?:ha|monitor|docker-[a-z0-9][a-z0-9._-]*)")

# grant and revoke give an account one private proxy, or take it back: they carry the proxy's name
GRANT_OPS = ("grant", "revoke")
OPS = ("add", "rotate", "enable", "disable", "remove", "admin-on", "admin-off") + GRANT_OPS
# ops that take access away: the admin confirms them by typing the account name
DESTRUCTIVE = ("rotate", "disable", "remove", "admin-off", "revoke")


class Refused(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def valid_name(name):
    if not isinstance(name, str) or not NAME_RE.fullmatch(name) or ":" in name or ">>>" in name:
        return False
    return not name.startswith("svc-") or bool(SERVICE_RE.fullmatch(name))


def kind(name):
    return "service" if name.startswith("svc-") else "user"


def actions(row, status=None):
    """The ops the admins' list offers for an account (a status.AccountRow); with the status, grant and revoke."""
    if row.protected:
        return []
    ops = ["rotate", "disable" if row.enabled else "enable"]
    if row.kind == "user":
        ops.append("admin-off" if row.admin else "admin-on")
        if status is not None and proxy_choices("grant", row, status):
            ops.append("grant")
        if row.proxies:
            ops.append("revoke")
    return ops + ["remove"]


def proxy_choices(op, row, status):
    """The proxies a grant or revoke may name for an account row: the private proxies it may not use yet, or the
    proxies granted to it (also one that is no longer private)."""
    if row is None or row.kind != "user":
        return []
    if op == "revoke":
        return list(row.proxies)
    return [] if row.admin else [name for name in status.private_proxies if name not in row.proxies]


def valid_proxy(proxy):
    return isinstance(proxy, str) and len(proxy) <= MAX_PROXY_NAME and bool(PROXY_NAME_RE.fullmatch(proxy))


def check(op, username, status, proxy=None):
    """
    Raise Refused unless op may be requested for username, given the applier's last status (proxy_status). A grant
    or revoke needs the proxy (check_account checks all but that); no other op takes one.
    """
    row = check_account(op, username, status)
    if op not in GRANT_OPS:
        if proxy is not None:
            raise Refused(_("Only a private proxy change names a proxy."))
        return
    if not valid_proxy(proxy):
        raise Refused(_("Choose a private proxy."))
    if op == "grant" and proxy not in status.private_proxies:
        raise Refused(_("%(proxy)s is not a private proxy.") % {"proxy": proxy})
    if op == "grant" and proxy in row.proxies:
        raise Refused(_("%(name)s may use %(proxy)s already.") % {"name": username, "proxy": proxy}, 409)
    if op == "revoke" and proxy not in row.proxies:
        raise Refused(_("%(name)s was not given %(proxy)s.") % {"name": username, "proxy": proxy}, 409)


def check_account(op, username, status):
    """check without the proxy of a grant or revoke; returns the account's row (None for an add)."""
    if op not in OPS:
        raise Refused(_("Unknown operation."))
    if not valid_name(username):
        raise Refused(_("Invalid account name: lowercase letters, digits, '.', '_' and '-', starting with a letter "
                        "or digit, at most 64 characters; svc- only as svc-docker-<node>."))
    if username in PROTECTED:
        raise Refused(_("%(name)s can only be changed outside the portal.") % {"name": username}, 403)
    if not status.available:
        raise Refused(_("Account information is unavailable right now, so no change can be requested."), 503)
    row = next((r for r in status.all_accounts if r.name == username), None)
    if row is not None and row.protected:
        raise Refused(_("%(name)s can only be changed outside the portal.") % {"name": username}, 403)
    if op == "add":
        if row is not None:
            raise Refused(_("The account %(name)s exists.") % {"name": username}, 409)
        return
    if row is None:
        raise Refused(_("There is no account %(name)s.") % {"name": username}, 404)
    if op in ("admin-on", "admin-off") and kind(username) == "service":
        raise Refused(_("A service account cannot be an admin account."))
    if op in GRANT_OPS and kind(username) == "service":
        raise Refused(_("A service account cannot be given a private proxy."))
    if op == "grant" and row.admin:
        raise Refused(_("%(name)s is an admin account: it may use every private proxy already.") % {"name": username},
                      409)
    return row


def suggestions(status, nodes):
    """svc-docker-<node> names without an account yet, for the add form."""
    existing = {r.name for r in status.all_accounts}
    return [f"svc-docker-{node}" for node in nodes if valid_name(f"svc-docker-{node}")
            and f"svc-docker-{node}" not in existing]


def submit(op, username, requested_by, self_service=False, proxy=None):
    """Record and write one request, already checked. Raises Refused (503) when the spool cannot take it."""
    from .models import AccountRequest

    row = AccountRequest.objects.create(id=spool.new_id(), op=op, username=username, requested_by=requested_by,
                                        self_service=self_service, proxy=proxy)
    try:
        spool.write_request(row.id, op, username, proxy)
    except OSError as e:
        request_id = row.id
        row.delete()
        log.error("request %s %s %s by %s: not written: %s", request_id, op, username, requested_by,
                  type(e).__name__)
        raise Refused(_("The request could not be saved. Nothing changed."), 503)
    log.info("request %s: %s %s%s by %s%s", row.id, op, username, f" {proxy}" if proxy else "", requested_by,
             " (self-service)" if self_service else "")
    return row


def _granted(row, status):
    """The private proxies granted to an account row (or a status.Account) that are private proxies now."""
    return [name for name in getattr(row, "proxies", None) or [] if name in status.private_proxies]


def restart_text(op, row, status, own=False, proxy=None):
    """What applying op restarts, for an account row (a status.AccountRow or, own, a status.Account; None for a new
    account). own: for the account's user, who sees only the proxies they may use: a user without an admin account
    uses one auto-switching proxy and the private proxies granted to it. The open proxies never restart: they take
    no accounts. A grant or revoke for an enabled account that is not an admin restarts only that proxy; otherwise it
    changes no proxy's settings and restarts nothing."""
    open_proxies = names(status.open_proxies)
    if op in GRANT_OPS:
        if row is not None and row.enabled and not row.admin:
            return _("Applying restarts %(proxy)s. Established connections through it drop, and new ones fail for a "
                     "few seconds.") % {"proxy": proxy}
        return _("Applying restarts no proxy: the change takes effect at %(proxy)s once the account's proxy access "
                 "is on and it is not an admin account.") % {"proxy": proxy}
    granted = _granted(row, status) if row is not None and not row.admin and op != "add" else []
    if own and not (row is not None and row.admin):
        if granted and status.raw_auth and status.open_proxies:
            return _("Applying restarts the Auto-switching Proxy, every public proxy but %(open)s, and %(proxies)s. "
                     "Established connections through them drop, and new ones fail for a few seconds.") % {
                "open": open_proxies, "proxies": names(granted)}
        if granted and status.raw_auth:
            return _("Applying restarts the Auto-switching Proxy, every public proxy and %(proxies)s. Established "
                     "connections through them drop, and new ones fail for a few seconds.") % {
                "proxies": names(granted)}
        if granted:
            return _("Applying restarts the Auto-switching Proxy and %(proxies)s. Established connections through "
                     "them drop, and new ones fail for a few seconds.") % {"proxies": names(granted)}
        if status.raw_auth and status.open_proxies:
            return _("Applying restarts the Auto-switching Proxy and every public proxy but %(open)s. Established "
                     "connections through them drop, and new ones fail for a few seconds.") % {"open": open_proxies}
        if status.raw_auth:
            return _("Applying restarts the Auto-switching Proxy and every public proxy. Established connections "
                     "through them drop, and new ones fail for a few seconds.")
        return _("Applying restarts the Auto-switching Proxy. Established connections through it drop, and new ones "
                 "fail for a few seconds.")
    if status.raw_auth and status.open_proxies:
        services = _("both auto-switching proxies and every proxy but %(open)s") % {"open": open_proxies}
    elif status.raw_auth:
        services = _("both auto-switching proxies and every proxy")
    elif op in ("admin-on", "admin-off") or (row is not None and row.admin):
        services = _("both auto-switching proxies and the private proxies")
    elif granted:
        services = _("both auto-switching proxies and %(proxies)s") % {"proxies": names(granted)}
    else:
        services = _("both auto-switching proxies")
    return _("Applying restarts %(services)s. Established connections through them drop, and new ones fail for a "
             "few seconds.") % {"services": services}
