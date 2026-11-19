"""
The applier's sanitized accounts status, status.json in its spool (proxy_accounts.py status --json): the accounts
without passwords, raw auth, the proxies with their access, whether they are open (never require an account) and
their ports, and the HA entries' ports. The applier writes
it after each of its runs from the accounts file; the portal only reads it.
"""

import datetime
import json
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from django.conf import settings

# proxy_accounts.py's account names; svc-* are service accounts, never a person's
NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
PROXY_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
PROTOCOLS = ("http", "socks")
# changed with proxy_accounts.py only, never from the portal
PROTECTED = ("svc-ha", "svc-monitor")


@dataclass
class Account:
    name: str
    enabled: bool
    admin: bool


@dataclass
class AccountRow:
    """An account as the admins' list shows it: users and service accounts."""
    name: str
    kind: str
    enabled: bool
    admin: bool
    protected: bool


@dataclass
class Proxy:
    name: str
    public: bool
    ports: Dict[str, Optional[int]]
    # an open proxy's own entry never requires an account, also with raw auth; only a public proxy can be open
    open: bool = False

    def requires_account(self, raw_auth: bool) -> bool:
        return not self.public or (raw_auth and not self.open)


@dataclass
class ProxyStatus:
    available: bool
    stale: bool = False
    generated_at: Optional[datetime.datetime] = None
    raw_auth: bool = False
    accounts: Dict[str, Account] = field(default_factory=dict)  # users only: a person's own account
    all_accounts: List[AccountRow] = field(default_factory=list)  # users and service accounts, for the admins
    proxies: List[Proxy] = field(default_factory=list)
    entry_ports: Dict[str, Dict[str, Optional[int]]] = field(default_factory=dict)
    # the accounts file differs from the applier's last apply (a change made with proxy_accounts.py): what it
    # holds may not be running yet
    unapplied_changes: Optional[bool] = None

    @property
    def open_proxies(self) -> List[str]:
        """The open proxies: their own entries never require an account."""
        return sorted(proxy.name for proxy in self.proxies if proxy.open)


def _port(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 65536 else None


def _ports(value):
    value = value if isinstance(value, dict) else {}
    return {protocol: _port(value.get(protocol)) for protocol in PROTOCOLS}


def parse(data, now=None):
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
        return ProxyStatus(available=False)
    generated = None
    try:
        generated = datetime.datetime.fromisoformat(str(data.get("generated_at")).replace("Z", "+00:00"))
        if generated.tzinfo is None:
            generated = None
    except ValueError:
        pass
    now = time.time() if now is None else now
    stale = generated is None or now - generated.timestamp() > settings.STATUS_FILE_MAX_AGE
    accounts, rows = {}, []
    for entry in data["accounts"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        name = entry["name"]
        if NAME_RE.fullmatch(name):
            kind = "service" if name.startswith("svc-") else "user"
            rows.append(AccountRow(name=name, kind=kind, enabled=entry.get("enabled") is True,
                                   admin=entry.get("admin") is True,
                                   protected=entry.get("protected") is True or name in PROTECTED))
        if not NAME_RE.fullmatch(name) or entry.get("kind") != "user" or name.startswith("svc-"):
            continue  # service accounts are nobody's own account
        accounts[name] = Account(name=name, enabled=entry.get("enabled") is True, admin=entry.get("admin") is True)
    proxies = []
    for entry in data.get("proxies") or []:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str) and PROXY_NAME_RE.fullmatch(entry["name"]):
            public = entry.get("access") == "public"
            proxies.append(Proxy(name=entry["name"], public=public, ports=_ports(entry.get("ports")),
                                 open=public and entry.get("open") is True))
    entries = data.get("entries") if isinstance(data.get("entries"), dict) else {}
    entry_ports = {name: _ports((entries.get(name) or {}).get("ports") if isinstance(entries.get(name), dict)
                                else None)
                   for name in ("ha-general", "ha-private")}
    unapplied = data.get("unapplied_changes")
    return ProxyStatus(available=True, stale=stale, generated_at=generated, raw_auth=data.get("raw_auth") is True,
                       accounts=accounts, all_accounts=sorted(rows, key=lambda r: r.name), proxies=proxies,
                       entry_ports=entry_ports, unapplied_changes=unapplied if isinstance(unapplied, bool) else None)


def load():
    try:
        with open(settings.STATUS_FILE, "rb") as f:
            raw = f.read(4 * 1024 * 1024)
        return parse(json.loads(raw.decode("utf-8")))
    except (OSError, ValueError):
        return ProxyStatus(available=False)
