"""
The applier's sanitized accounts status, status.json in its spool (proxy_accounts.py status --json): the accounts
without passwords, raw auth, the lines with their access and ports, and the HA entries' ports. The applier writes
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
LINE_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
PROTOCOLS = ("http", "socks")


@dataclass
class Account:
    name: str
    enabled: bool
    admin: bool


@dataclass
class Line:
    name: str
    public: bool
    ports: Dict[str, Optional[int]]


@dataclass
class ProxyStatus:
    available: bool
    stale: bool = False
    generated_at: Optional[datetime.datetime] = None
    raw_auth: bool = False
    accounts: Dict[str, Account] = field(default_factory=dict)
    lines: List[Line] = field(default_factory=list)
    entry_ports: Dict[str, Dict[str, Optional[int]]] = field(default_factory=dict)
    # the accounts file differs from the applier's last apply (a change made with proxy_accounts.py): what it
    # holds may not be running yet
    unapplied_changes: Optional[bool] = None


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
    accounts = {}
    for entry in data["accounts"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        name = entry["name"]
        if not NAME_RE.fullmatch(name) or entry.get("kind") != "user" or name.startswith("svc-"):
            continue  # service accounts are nobody's own account
        accounts[name] = Account(name=name, enabled=entry.get("enabled") is True, admin=entry.get("admin") is True)
    lines = []
    for entry in data.get("lines") or []:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str) and LINE_RE.fullmatch(entry["name"]):
            lines.append(Line(name=entry["name"], public=entry.get("access") == "public",
                              ports=_ports(entry.get("ports"))))
    entries = data.get("entries") if isinstance(data.get("entries"), dict) else {}
    entry_ports = {name: _ports((entries.get(name) or {}).get("ports") if isinstance(entries.get(name), dict)
                                else None)
                   for name in ("ha-general", "ha-all")}
    return ProxyStatus(available=True, stale=stale, generated_at=generated, raw_auth=data.get("raw_auth") is True,
                       accounts=accounts, lines=lines, entry_ports=entry_ports,
                       unapplied_changes=data.get("unapplied_changes") if isinstance(data.get("unapplied_changes"),
                                                                                   bool) else None)


def load():
    try:
        with open(settings.STATUS_FILE, "rb") as f:
            raw = f.read(4 * 1024 * 1024)
        return parse(json.loads(raw.decode("utf-8")))
    except (OSError, ValueError):
        return ProxyStatus(available=False)
