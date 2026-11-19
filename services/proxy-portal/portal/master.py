"""
Checks a Determined username and password once against the master, then ends the master session it opened.

POST <master>/api/v1/auth/login answers {"token", "user": {"username", "admin", "active", ...}}: 401 for wrong
credentials, 403 for an inactive user. The portal keeps neither the password nor the token: it calls
POST /api/v1/auth/logout with the token right away and returns only the username and the admin flag.

For an admin, before that logout, it also reads GET /api/v1/users with the same token: the username and the active
and admin flags of every cluster user, for the admins' Users page. A failure there does not fail the sign-in.
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional, Tuple

from django.conf import settings
from django.views.decorators.debug import sensitive_variables

from .upstream import HTTPStatusError, UpstreamError, request_json

log = logging.getLogger("portal.signin")

OK = "ok"
INVALID = "invalid"        # wrong username or password, or a remote (SSO) user
INACTIVE = "inactive"      # the Determined account is deactivated
UNAVAILABLE = "unavailable"  # the master did not answer as expected: not counted as a failed attempt


@dataclass(frozen=True)
class SignIn:
    result: str
    username: Optional[str] = None
    is_admin: bool = False
    # an admin's sign-in: ((username, active, admin), ...) from the master, or None when it could not be read
    users: Optional[Tuple[Tuple[str, bool, bool], ...]] = None


USERS_PAGE = 500
USERS_MAX = 20000
USERS_SECONDS = 15  # for the whole list: the sign-in waits for it, and nginx for the sign-in


def _parse_users(answer):
    """The users of one GET /api/v1/users answer and its total, or None for anything unexpected."""
    if not isinstance(answer, dict) or not isinstance(answer.get("users"), list):
        return None
    users = []
    for entry in answer["users"]:
        if not isinstance(entry, dict):
            return None
        name, active, admin = entry.get("username"), entry.get("active"), entry.get("admin")
        if not isinstance(name, str) or not name or len(name) > 128 or not isinstance(active, bool) \
                or not isinstance(admin, bool):
            return None
        users.append((name, active, admin))
    total = (answer.get("pagination") or {}).get("total") if isinstance(answer.get("pagination"), dict) else None
    if total is not None and (not isinstance(total, int) or isinstance(total, bool) or total < 0):
        return None
    return users, total


@sensitive_variables("token")
def _cluster_users(token):
    """Every cluster user, page by page, read with the admin's token; None on any failure (logged without it)."""
    try:
        return _read_users(token)
    except Exception as e:  # nothing the list read raises may fail the sign-in
        log.warning("Determined user list at an admin sign-in: %s; the previous list is kept", type(e).__name__)
        return None


def _too_slow():
    log.warning("Determined user list at an admin sign-in: not read within %d seconds; the previous list is kept",
                USERS_SECONDS)


@sensitive_variables("token")
def _read_users(token):
    users, seen = [], set()
    deadline = time.monotonic() + USERS_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _too_slow()
        try:
            # the timeout bounds the whole answer (upstream.request_json), however slowly it comes
            answer = request_json(f"{settings.MASTER_URL}/api/v1/users?offset={len(users)}&limit={USERS_PAGE}",
                                  headers={"Authorization": "Bearer " + token},
                                  timeout=min(settings.UPSTREAM_TIMEOUT, remaining))
        except UpstreamError as e:
            log.warning("Determined user list at an admin sign-in: %s; the previous list is kept", e)
            return None
        if time.monotonic() >= deadline:  # not even the last page counts once the time is up
            return _too_slow()
        parsed = _parse_users(answer)
        if parsed is None:
            log.warning("Determined user list at an admin sign-in: an unexpected answer; the previous list is kept")
            return None
        page, total = parsed
        names = {name for name, _, _ in page}
        if len(names) != len(page) or names & seen:
            log.warning("Determined user list at an admin sign-in: repeated names; the previous list is kept")
            return None
        seen |= names
        users.extend(page)
        if not page or (len(users) >= total if total is not None else len(page) < USERS_PAGE):
            break
        if len(users) >= USERS_MAX:
            log.warning("Determined user list at an admin sign-in: more than %d users; the previous list is kept",
                        USERS_MAX)
            return None
    if not users:
        log.warning("Determined user list at an admin sign-in: empty; the previous list is kept")
        return None
    return tuple(users)


def _logout(token):
    try:
        request_json(settings.MASTER_URL + "/api/v1/auth/logout", data={},
                     headers={"Authorization": "Bearer " + token})
        return True
    except UpstreamError as e:
        # The session stays valid on the master until it expires; the portal still keeps no copy.
        log.warning("Determined logout after a sign-in check failed: %s", e)
        return False


@sensitive_variables("password", "token", "answer")
def check_credentials(username, password):
    token = None
    try:
        answer = request_json(settings.MASTER_URL + "/api/v1/auth/login",
                              data={"username": username, "password": password, "isHashed": False})
    except HTTPStatusError as e:
        if e.status in (400, 401):
            return SignIn(INVALID)
        if e.status == 403:
            return SignIn(INACTIVE)
        log.warning("Determined login check: %s", e)
        return SignIn(UNAVAILABLE)
    except UpstreamError as e:
        log.warning("Determined login check: master not reachable (%s)", e)
        return SignIn(UNAVAILABLE)
    try:
        token = answer.get("token") if isinstance(answer, dict) else None
        user = answer.get("user") if isinstance(answer, dict) else None
        if not isinstance(user, dict) or not isinstance(user.get("username"), str) or not user["username"]:
            log.warning("Determined login check: an answer without a user")
            return SignIn(UNAVAILABLE)
        if user.get("active") is not True:
            return SignIn(INACTIVE)
        is_admin = user.get("admin") is True
        users = _cluster_users(token) if is_admin and isinstance(token, str) and token else None
        return SignIn(OK, username=user["username"], is_admin=is_admin, users=users)
    finally:
        if isinstance(token, str) and token:
            _logout(token)
