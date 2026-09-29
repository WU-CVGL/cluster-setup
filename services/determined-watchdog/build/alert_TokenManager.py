"""Determined session token shared by the watchdog and Prometheus.

The watchdog logs in to Determined and writes the session token to
DETERMINED_METRICS_TOKEN_FILE (default /run/determined-metrics/token; host: the file
"token" in DET_METRICS_SECRETS_DIR from services/.env, default
services/prometheus/secrets/; see ../../README.md section 6.1 and
../../prometheus/README.md) with
metrics_token.write_metrics_token (atomic replace, mode 0600, token + newline).
Prometheus mounts the same directory read-only and reads the file through
`authorization.credentials_file` in its det-master job on every scrape, so no
YAML edit, reload or restart is needed on rotation. The watchdog's own
Determined API calls use the same token.

The token is renewed at start and on every hourly check when the file is
missing/unreadable, when its expiry cannot be decoded, or when it expires in
less than Config.token_renew_hours; when Determined answers GET /api/v1/me with
HTTP 401 for the file token (checked at start and at every hourly check, so a
revoked session recovers even though the det-master scrape, and the Grafana alert
built on it, are down); and once, immediately, when Determined answers the
watchdog's own API calls with HTTP 401 (ensure_token(force=True)).
If a login succeeds but the file cannot be written, the watchdog keeps using the
new session and later checks retry only the write.
Slack: a renewal posts "Automatic update success ~", a failed one "Automatic update
FAILED ~" (hourly checks, including a renewal after HTTP 401 from /api/v1/me, and
the forced renewal). The start-up check (ensure_token(notify=False)) posts neither,
whatever the reason for renewing, and only logs, so a restart is silent; a failed
start-up renewal is still pending, so the next hourly check retries it and notifies
as usual.
Determined issues PASETO v2.public tokens:
"v2.public.<base64url(JSON payload + 64-byte signature)>[.<base64url footer>]";
the payload carries the session expiry ("expiry", RFC 3339).
"""
import base64
import binascii
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import urljoin

import requests

from alert_config import HTTP_TIMEOUT, Config, log, redact
from alert_MessageNotifier import MessageNotifier
from metrics_token import write_metrics_token

PASETO_V2_SIGNATURE_BYTES = 64  # Ed25519 signature appended to the payload

_RFC3339 = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[Tt ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?([Zz]|[+-]\d{2}:?\d{2})?$"
)


class TokenError(Exception):
    """The Determined login did not produce a usable token."""


def parse_timestamp(value) -> Optional[datetime]:
    """RFC 3339 string (any number of fraction digits, Z or offset) or epoch seconds."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    match = _RFC3339.match(value.strip())
    if not match:
        return None
    date, clock, fraction, offset = match.groups()
    fraction = (fraction or "")[:6].ljust(6, "0")
    if offset is None or offset in ("Z", "z"):
        offset = "+00:00"
    elif ":" not in offset:
        offset = offset[:3] + ":" + offset[3:]
    try:
        return datetime.fromisoformat(f"{date}T{clock}.{fraction}{offset}")
    except ValueError:
        return None


def decode_paseto_payload(token) -> Optional[dict]:
    """Return the JSON claims of a v2.public token (signature NOT verified), or None."""
    if not isinstance(token, str):
        return None
    parts = token.strip().split(".")
    if len(parts) not in (3, 4) or parts[0] != "v2" or parts[1] != "public":
        return None
    body = parts[2]
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except (binascii.Error, ValueError):
        return None
    if len(raw) <= PASETO_V2_SIGNATURE_BYTES:
        return None
    try:
        payload = json.loads(raw[:-PASETO_V2_SIGNATURE_BYTES].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def token_expiry(token) -> Optional[datetime]:
    payload = decode_paseto_payload(token)
    if payload is None:
        return None
    # Determined's session claims use "expiry"; "exp" is the PASETO registered claim.
    for key in ("expiry", "exp"):
        if key in payload:
            return parse_timestamp(payload[key])
    return None


def renewal_reason(token: Optional[str], now: datetime, margin: timedelta) -> Optional[str]:
    """None when the token can be kept, otherwise why it must be renewed."""
    if not token:
        return "token file is missing, unreadable or empty"
    expiry = token_expiry(token)
    if expiry is None:
        return "cannot decode the token expiry"
    if expiry - now < margin:
        return f"token expires at {expiry.isoformat()} (less than {margin} from now)"
    return None


def read_token(path) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as f:
            token = f.read().strip()
    except (OSError, UnicodeDecodeError):
        return None
    return token or None


def token_rejected(det_web: str, token: str) -> bool:
    """True only when Determined answers GET /api/v1/me with HTTP 401 for this token.

    Any other outcome (2xx, another status, a network error) keeps the token, so an
    unreachable master never triggers a login at every check.
    """
    try:
        response = requests.get(
            urljoin(det_web, "api/v1/me"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=HTTP_TIMEOUT,
        )
    except requests.exceptions.RequestException:
        return False
    return response.status_code == 401


def det_login(det_web: str, username: str, password: str) -> str:
    det_login_api = urljoin(det_web, "api/v1/auth/login/")
    payload = json.dumps({"username": username, "password": password})
    headers = {"Content-Type": "application/json"}
    try:
        response = requests.post(det_login_api, headers=headers, data=payload, timeout=HTTP_TIMEOUT)
    except requests.exceptions.RequestException as e:
        raise TokenError(f"cannot reach the Determined master ({type(e).__name__}: {e})") from e
    if not 200 <= response.status_code < 300:
        raise TokenError(f"Determined login returned HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        raise TokenError("Determined login returned a non-JSON response") from None
    token = body.get("token") if isinstance(body, dict) else None
    # Same rule as metrics_token.write_metrics_token: a non-empty token without whitespace.
    if not isinstance(token, str) or not token or any(c.isspace() for c in token):
        raise TokenError("Determined login did not return a valid token")
    return token


class TokenManager:
    def __init__(
        self,
        config: Config,
        message_notifier: MessageNotifier,
        now: Optional[Callable[[], datetime]] = None,
    ):
        self.config = config
        self.message_notifier = message_notifier
        self.path = Path(config.det_metrics_token_path)
        self.margin = timedelta(hours=config.token_renew_hours)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._token: Optional[str] = None
        # (new token, file token seen at its login) after a login whose write failed.
        self._unwritten: Optional[Tuple[str, Optional[str]]] = None

    @property
    def token(self) -> Optional[str]:
        return self._token

    def det_headers(self) -> Dict[str, str]:
        if not self._token:
            return {}
        return {"Authorization": f"Bearer {self._token}"}

    def _redact(self, text) -> str:
        return redact(text, self.config.secret_values() + [self._token])

    def _renewal_failed(self, error: Exception, next_step: str, notify: bool) -> None:
        error_text = self._redact(f"{type(error).__name__}: {error}")
        silent = "" if notify else " [start-up check: not posted to Slack]"
        log(f"Determined token renewal FAILED ({error_text}); {next_step}{silent}")
        if notify:
            self.message_notifier.send_slack_warning(
                warning_type="ERROR",
                info=f"Automatic update FAILED ~ ({error_text})",
                slack_webhook_url=self.config.slack_webhook_url,
            )

    def _retry_unwritten(self, token: str, notify: bool) -> bool:
        """Write a session obtained at an earlier check whose write failed; no new login."""
        log(f"Retrying the write of the Determined token obtained earlier to {self.path}.")
        try:
            write_metrics_token(self.path, token)
        except Exception as e:
            self._renewal_failed(
                e,
                f"{self.path} left unchanged; the watchdog keeps using the session obtained "
                "earlier and retries the write at the next hourly check.",
                notify,
            )
            return False
        self._unwritten = None
        log(f"Wrote the Determined token obtained earlier to {self.path}.")
        return True

    def ensure_token(self, force: bool = False, notify: bool = True) -> bool:
        """Renew the token if needed. Never raises.

        force=True renews even though the expiry is still valid: Determined rejected
        the token (HTTP 401), e.g. because the session was revoked.
        notify=False (the start-up check) logs the outcome without posting it to
        Slack; a failed renewal stays pending, so the next check retries it.
        If a login succeeds but the file cannot be written, the new session is kept
        for the watchdog's own calls, and later checks retry only the write (no new
        login) while the file still holds the token seen at that login and the new
        session needs no renewal. A token placed in the file meanwhile is used instead.
        Returns True when the token file holds a token that does not need renewal.
        """
        try:
            file_token = read_token(self.path)
            reason = renewal_reason(file_token, self._now(), self.margin)
        except Exception as e:
            file_token, reason = None, f"cannot check the token file ({type(e).__name__})"
        if force:
            # The token in use was just rejected: an unwritten session is not worth writing.
            self._unwritten = None
            reason = "Determined rejected the token (HTTP 401) before its expiry"
        elif self._unwritten is not None:
            pending, file_token_seen = self._unwritten
            if file_token == file_token_seen:
                try:
                    pending_reason = renewal_reason(pending, self._now(), self.margin)
                except Exception as e:
                    pending_reason = f"cannot check the unwritten token ({type(e).__name__})"
                if pending_reason is None:
                    self._token = pending
                    return self._retry_unwritten(pending, notify)
                reason = f"the unwritten token obtained earlier needs renewal: {pending_reason}"
            else:
                log(f"{self.path} was replaced since the failed write; using the token in the file.")
                self._unwritten = None
        if reason is None and token_rejected(self.config.det_web, file_token):
            # A revoked session breaks the det-master scrape, and with it the Grafana alert
            # that would otherwise make the watchdog call Determined and see the 401.
            reason = "Determined rejected the token (HTTP 401 from /api/v1/me) before its expiry"
        if reason is None:
            self._token = file_token
            log(f"Determined token OK (expires at {token_expiry(file_token).isoformat()}).")
            return True
        if file_token and not self._token:
            self._token = file_token  # keep using the old token until a renewal succeeds

        log(f"Renewing the Determined token: {reason}.")
        try:
            new_token = det_login(
                self.config.det_web, self.config.det_username, self.config.det_password
            )
        except Exception as e:
            self._renewal_failed(
                e, f"{self.path} left unchanged, retrying at the next hourly check.", notify
            )
            return False
        # The new session is valid even if the file cannot be written below.
        self._token = new_token
        try:
            write_metrics_token(self.path, new_token)
        except Exception as e:
            self._unwritten = (new_token, file_token)
            self._renewal_failed(
                e,
                f"{self.path} left unchanged; the watchdog uses the new session itself "
                "and retries only the write at the next hourly check.",
                notify,
            )
            return False
        self._unwritten = None

        expiry = token_expiry(new_token)
        if expiry is None:
            # Renewed at every hourly check until the format is understood: log, don't spam Slack.
            log(
                f"Obtained new Determined token, written to {self.path}, but its expiry "
                "cannot be decoded: it will be renewed at every hourly check."
            )
            return True
        log(f"Obtained new Determined token (expires at {expiry.isoformat()}), written to {self.path}.")
        if notify:
            self.message_notifier.send_slack_warning(
                warning_type="notification",
                info="Automatic update success ~",
                slack_webhook_url=self.config.slack_webhook_url,
            )
        return True
