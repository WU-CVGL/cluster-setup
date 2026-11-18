"""
The admins' account operations: which requests the portal may write, checked before any file is written. The
applier checks every request again (applier/proxy_apply.py); the accounts file stays the only record of accounts.
"""

import logging
import re

from django.utils.translation import gettext as _

from . import spool
from .proxy_status import PROTECTED

log = logging.getLogger("portal.requests")

# proxy_accounts.py's validate_name, which the image does not contain; tests/test_admin_requests.py compares the two
NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
SERVICE_RE = re.compile(r"svc-(?:ha|monitor|docker-[a-z0-9][a-z0-9._-]*)")

OPS = ("add", "rotate", "enable", "disable", "remove", "admin-on", "admin-off")
# ops that take access away: the admin confirms them by typing the account name
DESTRUCTIVE = ("rotate", "disable", "remove", "admin-off")


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


def actions(row):
    """The ops the admins' list offers for an account (a status.AccountRow)."""
    if row.protected:
        return []
    ops = ["rotate", "disable" if row.enabled else "enable"]
    if row.kind == "user":
        ops.append("admin-off" if row.admin else "admin-on")
    return ops + ["remove"]


def check(op, username, status):
    """Raise Refused unless op may be requested for username, given the applier's last status (proxy_status)."""
    if op not in OPS:
        raise Refused(_("Unknown operation."))
    if not valid_name(username):
        raise Refused(_("Invalid account name: lowercase letters, digits, '.', '_' and '-', starting with a letter "
                        "or digit, at most 64 characters; svc- only as svc-docker-<node>."))
    if username in PROTECTED:
        raise Refused(_("%(name)s can only be changed outside the portal.") % {"name": username}, 403)
    if not status.available:
        raise Refused(_("The accounts status is unavailable, so no change can be requested."), 503)
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


def suggestions(status, nodes):
    """svc-docker-<node> names without an account yet, for the add form."""
    existing = {r.name for r in status.all_accounts}
    return [f"svc-docker-{node}" for node in nodes if valid_name(f"svc-docker-{node}")
            and f"svc-docker-{node}" not in existing]


def submit(op, username, requested_by, self_service=False):
    """Record and write one request, already checked. Raises Refused (503) when the spool cannot take it."""
    from .models import AccountRequest

    row = AccountRequest.objects.create(id=spool.new_id(), op=op, username=username, requested_by=requested_by,
                                        self_service=self_service)
    try:
        spool.write_request(row.id, op, username)
    except OSError as e:
        request_id = row.id
        row.delete()
        log.error("request %s %s %s by %s: not written: %s", request_id, op, username, requested_by,
                  type(e).__name__)
        raise Refused(_("The request could not be saved. Nothing changed."), 503)
    log.info("request %s: %s %s by %s%s", row.id, op, username, requested_by, " (self-service)" if self_service
             else "")
    return row


def restart_text(op, row, status):
    """What applying op restarts, for an account row (a status.AccountRow, or None for a new account)."""
    if status.raw_auth:
        services = _("both auto-switching proxies and every proxy")
    elif op in ("admin-on", "admin-off") or (row is not None and row.admin):
        services = _("both auto-switching proxies and the private proxies")
    else:
        services = _("both auto-switching proxies")
    return _("Applying restarts %(services)s. Established connections through them drop, and new ones fail for a "
             "few seconds.") % {"services": services}
