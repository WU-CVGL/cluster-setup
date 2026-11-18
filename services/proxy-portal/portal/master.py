"""
Checks a Determined username and password once against the master, then ends the master session it opened.

POST <master>/api/v1/auth/login answers {"token", "user": {"username", "admin", "active", ...}}: 401 for wrong
credentials, 403 for an inactive user. The portal keeps neither the password nor the token: it calls
POST /api/v1/auth/logout with the token right away and returns only the username and the admin flag.
"""

import logging
from dataclasses import dataclass
from typing import Optional

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
        return SignIn(OK, username=user["username"], is_admin=user.get("admin") is True)
    finally:
        if isinstance(token, str) and token:
            _logout(token)
