"""
The state of an account request, from the applier's status file (applier/README.md#statuses):

    pending       written, no status yet
    applying      taken by the applier, in its current batch
    applied       live and verified (changed: false when nothing had to change)
    failed        not applied: stage, error, and restored (the previous state is back, or the rollback did not finish)
    unconfirmed   live, and every login that must be refused is refused, but the logins that must pass got no 204

The row keeps a copy of the outcome for the change log; the status file is the source.
"""

import datetime

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext as _

from . import spool

REVOKES = ("disable", "remove", "rotate", "admin-off")  # take an access away: the old one works until applied
REPLACES = ("add", "rotate", "remove")  # a live one makes an earlier one-time password of the account invalid
ROLLBACK_RESTARTS = ("swap", "restart", "verify", "commit")  # stages whose rollback restarts the entries
RESULT_RECHECK = datetime.timedelta(hours=1)  # a later request of the same run can supersede a result


def _parse_time(value):
    try:
        at = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return at if at.tzinfo else None
    except ValueError:
        return None


def settled(row, now):
    """
    Whether the row's outcome can no longer change: a final state, except a failure whose rollback had not
    finished (the applier's next run may still restore it) and, for an hour, a one-time password that a later
    request of the same run can supersede.
    """
    if not row.final or (row.state == "failed" and row.restored is not True):
        return False
    return not (row.gives_password and now - row.requested_at < RESULT_RECHECK)


def refresh(row, now=None):
    """Copy the status file's outcome into the row (saved when it changed). Returns the row."""
    now = now or timezone.now()
    if settled(row, now):
        return row
    data = spool.read_status(row.id)
    if data is None:
        return row
    state = data["status"] if data["status"] in ("applying", "applied", "failed", "unconfirmed") else row.state
    restarted = data.get("restarted")
    values = {
        "state": state,
        "stage": str(data.get("stage") or "")[:32],
        "error": str(data.get("error") or "")[:2000],
        "restored": data.get("restored") if isinstance(data.get("restored"), bool) else None,
        "changed": data.get("changed") if isinstance(data.get("changed"), bool) else None,
        "restarted": " ".join(s for s in restarted if isinstance(s, str))[:1000] if isinstance(restarted, list) else "",
        "result": data.get("result") if isinstance(data.get("result"), bool) else None,
        "superseded_by": str(data.get("superseded_by") or "")[:64],
        "status_at": _parse_time(data.get("updated_at")),
    }
    changed = [k for k, v in values.items() if getattr(row, k) != v]
    if changed:
        for k in changed:
            setattr(row, k, values[k])
        row.save(update_fields=changed)
    return row


def refresh_all(rows):
    return [refresh(row) for row in rows]


def level(row):
    return {"pending": "info", "applying": "info", "applied": "ok", "unconfirmed": "warn"}.get(row.state, "bad")


def describe(row, now=None):
    """One plain sentence or two on what the state means for the account's access."""
    now = now or timezone.now()
    revoke = row.op in REVOKES
    if row.state == "pending":
        text = _("Waiting for the applier. Not applied yet: the old access still works.") if revoke \
            else _("Waiting for the applier. Not applied yet.")
        waited = (now - row.requested_at).total_seconds()
        if waited > settings.REQUEST_PENDING_WARN:
            text += " " + _("Not taken after %(n)d minutes: the applier may not be running.") % {"n": waited // 60}
        return text
    if row.state == "applying":
        return _("Being applied; the affected entries restart. The revoke is not confirmed yet: the old access "
                 "may still work.") if revoke else _("Being applied; the affected entries restart.")
    if row.state == "applied":
        if row.changed is False and row.restarted:
            return _("Nothing to change: the account was already in that state. The entries restarted to apply "
                     "other changes.")
        if row.changed is False:
            return _("Nothing to change: the account was already in that state. Nothing restarted.")
        return _("Applied and verified.")
    if row.state == "unconfirmed":
        if row.op in ("disable", "remove", "admin-off"):
            return _("Live: the entries refuse the old access (verified), but no proxy answered the checks that "
                     "must pass in time, so the entries may not be passing traffic right now.")
        if row.op == "rotate":
            return _("Live: the old password is refused (verified). The new password is not confirmed yet: no "
                     "proxy answered in time.")
        return _("Live, but not confirmed yet: no proxy answered the new login in time (the proxies or the check "
                 "target may be down).")
    # failed
    if row.stage == "request":
        return _("Refused by the applier. Nothing changed.")
    if row.restored is False:
        return _("Failed, and the rollback did not finish: the entries may be down or in a mixed state. Check the "
                 "applier on the VM (journalctl -u proxy-apply).")
    text = _("Failed; the previous state is back: the account is unchanged and the old access still works.") \
        if revoke else _("Failed; the previous state is back: the account is unchanged.")
    if row.stage in ROLLBACK_RESTARTS:
        text += " " + _("The entries restarted: established connections dropped.")
    return text


def superseded_by(row):
    """
    The request that made this one-time password invalid: (id, op), op None when that request is unknown here.
    The applier marks it within one run (superseded_by); across runs, a later live add, rotate or remove of the
    same account does. None when there is none.
    """
    if row.superseded_by:
        later = type(row).objects.filter(id=row.superseded_by).first()
        return row.superseded_by, later.op if later else None
    rows = type(row).objects.filter(username=row.username, op__in=REPLACES, requested_at__gt=row.requested_at) \
        .exclude(id=row.id).exclude(state="failed").order_by("requested_at", "id")
    for later in rows:
        refresh(later)
        if later.state in ("applied", "unconfirmed") and later.changed is not False:
            return later.id, later.op
    return None


def password_state(row, username):
    """
    For an add or rotate: ready (the requesting admin may collect it), other (another admin's request), shown,
    expired, superseded, missing (no result was written), unreadable, waiting, or none (not applied); None otherwise.
    """
    if not row.gives_password:
        return None
    if row.result_state in ("shown", "expired", "unreadable"):
        return row.result_state
    if row.result_state == "claimed":
        return "shown"
    if row.state in ("pending", "applying"):
        return "waiting"
    if row.state == "failed":
        return "none"
    if row.superseded_by:
        return "superseded"
    if row.result is False:
        return "missing"
    if row.result is not True:
        return "waiting"
    if not spool.result_exists(row.id):
        return "expired"
    if superseded_by(row):
        return "superseded"
    return "ready" if row.requested_by == username else "other"


def password_text(state, row):
    return {
        "waiting": _("The new password is handed out once the change is live."),
        "ready": _("The new password is ready. It is shown once, to you only, and then deleted."),
        "other": _("Only %(admin)s, who made this request, can see the new password.") % {"admin": row.requested_by},
        "shown": _("The password was shown once; the portal does not keep it. For a new one, request a new "
                   "password."),
        "expired": _("The one-time password expired: results are deleted after 10 minutes. For a new one, request "
                     "a new password."),
        "superseded": _superseded_text(row) if state == "superseded" else "",
        "missing": _("No password was handed out for this request. For a new one, request a new password."),
        "unreadable": _("The one-time result could not be read and was deleted. For a new one, request a new "
                        "password."),
        "none": _("No password: the change was not applied."),
    }.get(state, "")


def _superseded_text(row):
    later_id, op = superseded_by(row) or (row.superseded_by, None)
    if op == "remove":
        return _("The account was removed by request %(id)s: this password no longer works.") % {"id": later_id}
    return _("A later request replaced this password: use the password of request %(id)s.") % {"id": later_id}


def as_json(row, username):
    pw = password_state(row, username)
    return {
        "id": row.id, "op": row.op, "username": row.username, "requested_by": row.requested_by,
        "requested_at": row.requested_at.isoformat(), "state": row.state, "final": row.final, "level": level(row),
        "text": describe(row), "stage": row.stage or None, "error": row.error or None, "restored": row.restored,
        "changed": row.changed, "restarted": row.restarted.split() if row.restarted else [],
        "status_updated_at": row.status_at.isoformat() if row.status_at else None,
        "password": pw, "password_text": password_text(pw, row) if pw else None,
    }
