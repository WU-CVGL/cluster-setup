#!/usr/bin/env python3
"""
Proxy accounts: one protected accounts file, from which every authenticated proxy config is rendered.

    proxy_accounts.py add NAME [--show]      new account with a random password
    proxy_accounts.py rotate NAME [--show]   new password for an account
    proxy_accounts.py remove NAME
    proxy_accounts.py list                   account names, without passwords
    proxy_accounts.py raw-auth [on|off]      show or set authentication on the raw lines (off by default)
    proxy_accounts.py render                 write the configs below from the accounts file

render writes, relative to services/ (all gitignored, mode 0600, replaced atomically):
  xray-ha/config/config.json     xray-ha/config.example.json with every account except svc-ha required on
                                 both inbounds; with raw auth, the pool outbounds log in to the lines as svc-ha
  blackbox/config/blackbox.yml   blackbox/blackbox.example.yml with svc-monitor in the proxy URLs of the HA
                                 entry, and of the raw lines with raw auth
  xray/*/config/config.json      with raw auth, every account required on the raw lines' inbounds; without
                                 it, inbounds that carry accounts back to no authentication, other files untouched

Account names are cluster usernames; names starting with svc- are service accounts:
svc-ha (the HA entry's login to the lines), svc-monitor (the probes), svc-docker-<node> (a node's Docker daemon).
"""

import argparse
import json
import os
import re
import secrets
import sys
import tempfile
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from XrayConfigHandler import XrayConfigHandler  # noqa: E402

SERVICES_DIR = Path(__file__).resolve().parents[2]
ACCOUNTS_FILE = Path("proxy-accounts/accounts.json")
HA_EXAMPLE = Path("xray-ha/config.example.json")
HA_CONFIG = Path("xray-ha/config/config.json")
BLACKBOX_EXAMPLE = Path("blackbox/blackbox.example.yml")
BLACKBOX_CONFIG = Path("blackbox/config/blackbox.yml")
RAW_CONFIG_GLOB = "xray/*/config/config.json"

RELAY = "svc-ha"
MONITOR = "svc-monitor"
POOL_PREFIX = "pool-"
HA_HOST = "xray-ha"

NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
SERVICE_RE = re.compile(r"svc-(?:ha|monitor|docker-[a-z0-9][a-z0-9._-]*)")
PROXY_URL_RE = re.compile(r"^(\s*proxy_url:\s*)(https?|socks5h?)://(?:[^@/\s]*@)?([A-Za-z0-9._-]+)(:\d+\s*)$")


class AccountsError(Exception):
    pass


def validate_name(name: str) -> str:
    if not NAME_RE.fullmatch(name) or ":" in name or ">>>" in name:
        raise AccountsError(f"invalid account name {name!r}: lowercase letters, digits, '.', '_', '-', "
                            "starting with a letter or digit, at most 64 characters")
    if name.startswith("svc-") and not SERVICE_RE.fullmatch(name):
        raise AccountsError(f"invalid service account {name!r}: svc- is reserved for "
                            f"{RELAY}, {MONITOR} and svc-docker-<node>")
    return name


def new_password() -> str:
    return secrets.token_urlsafe(24)


def write_private(path: Path, text: str) -> None:
    """Write text to path with mode 0600, replacing the file atomically (same directory, then rename)."""
    if not path.parent.exists():
        path.parent.mkdir(mode=0o700, parents=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class Accounts:
    """The accounts file: {"raw_auth": bool, "accounts": {name: password}}."""

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.path = self.base_dir / ACCOUNTS_FILE
        self.raw_auth = False
        self.accounts = {}  # type: Dict[str, str]
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.raw_auth = bool(data.get("raw_auth", False))
            self.accounts = dict(data.get("accounts") or {})
            for name in self.accounts:
                validate_name(name)

    def save(self) -> None:
        data = {"raw_auth": self.raw_auth, "accounts": dict(sorted(self.accounts.items()))}
        write_private(self.path, json.dumps(data, indent=2) + "\n")

    def require(self, name: str) -> None:
        if name not in self.accounts:
            raise AccountsError(f"no account {name!r}")

    def pairs(self, exclude: Tuple[str, ...] = ()) -> List[Tuple[str, str]]:
        return [(name, pw) for name, pw in sorted(self.accounts.items()) if name not in exclude]


def render_ha(example: dict, accounts: Accounts) -> dict:
    config = json.loads(json.dumps(example))
    XrayConfigHandler.set_inbound_users(config, accounts.pairs(exclude=(RELAY,)))
    pool = [o for o in config.get("outbounds") or [] if str(o.get("tag", "")).startswith(POOL_PREFIX)]
    if not pool:
        raise AccountsError(f"{HA_EXAMPLE} has no {POOL_PREFIX}* outbounds")
    for outbound in pool:
        for server in outbound["settings"]["servers"]:
            if accounts.raw_auth:
                server["users"] = [{"user": RELAY, "pass": accounts.accounts[RELAY]}]
            else:
                server.pop("users", None)
    return config


def render_blackbox(example: str, accounts: Accounts) -> str:
    userinfo = (urllib.parse.quote(MONITOR, safe="") + ":"
                + urllib.parse.quote(accounts.accounts[MONITOR], safe="") + "@")
    lines, ha_urls = [], 0
    for line in example.splitlines(True):
        match = PROXY_URL_RE.match(line.rstrip("\n"))
        if match:
            prefix, scheme, host, rest = match.groups()
            ha_urls += host == HA_HOST
            auth = host == HA_HOST or (accounts.raw_auth and host.startswith("xray-"))
            line = f"{prefix}{scheme}://{userinfo if auth else ''}{host}{rest}\n"
        elif "proxy_url:" in line:
            raise AccountsError(f"{BLACKBOX_EXAMPLE}: cannot parse {line.strip()!r}")
        lines.append(line)
    if not ha_urls:
        raise AccountsError(f"{BLACKBOX_EXAMPLE} has no proxy_url through {HA_HOST}")
    header = (f"# Rendered by xray/scripts/proxy_accounts.py from {BLACKBOX_EXAMPLE.as_posix()}; "
              "holds credentials, not in git.\n")
    return header + "".join(lines)


def render(accounts: Accounts) -> List[Path]:
    """Render every config first, then write them; returns the files written."""
    base = accounts.base_dir
    needed = [MONITOR] + ([RELAY] if accounts.raw_auth else [])
    for name in needed:
        if name not in accounts.accounts:
            raise AccountsError(f"service account {name} is missing: run 'add {name}' first")
    outputs = []  # type: List[Tuple[Path, str]]
    ha = render_ha(json.loads((base / HA_EXAMPLE).read_text(encoding="utf-8")), accounts)
    outputs.append((base / HA_CONFIG, json.dumps(ha, indent=4) + "\n"))
    outputs.append((base / BLACKBOX_CONFIG,
                    render_blackbox((base / BLACKBOX_EXAMPLE).read_text(encoding="utf-8"), accounts)))
    raw_users = accounts.pairs() if accounts.raw_auth else None
    for path in sorted(base.glob(RAW_CONFIG_GLOB)):
        old = json.loads(path.read_text(encoding="utf-8"))
        if raw_users is None and not any("users" in (i.get("settings") or {}) for i in old.get("inbounds") or []
                                         if i.get("protocol") in ("http", "socks")):
            continue  # raw auth off and no accounts there: leave the line's config as it is
        new = json.loads(json.dumps(old))
        try:
            XrayConfigHandler.set_inbound_users(new, raw_users)
        except ValueError as e:
            raise AccountsError(f"{path.relative_to(base)}: {e}")
        if new != old:
            # The format create_xray_service.py writes
            outputs.append((path, json.dumps(new, indent=4, ensure_ascii=False)))
    written = []
    for path, text in outputs:
        if path.exists() and path.read_text(encoding="utf-8") == text:
            continue
        write_private(path, text)
        written.append(path)
    return written


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Proxy accounts and the configs rendered from them.")
    parser.add_argument("--base-dir", type=Path, default=SERVICES_DIR, help="services/ directory (default: %(default)s)")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("add", "rotate"):
        p = sub.add_parser(command)
        p.add_argument("name")
        p.add_argument("--show", action="store_true", help="print the new password")
    sub.add_parser("remove").add_argument("name")
    sub.add_parser("list")
    sub.add_parser("raw-auth").add_argument("state", nargs="?", choices=("on", "off"))
    sub.add_parser("render")
    args = parser.parse_args(argv)

    try:
        accounts = Accounts(args.base_dir)
        if args.command in ("add", "rotate", "remove"):
            name = validate_name(args.name)
            if args.command == "add" and name in accounts.accounts:
                raise AccountsError(f"account {name!r} exists; use rotate")
            if args.command != "add":
                accounts.require(name)
            if args.command == "remove":
                del accounts.accounts[name]
            else:
                accounts.accounts[name] = new_password()
            accounts.save()
            if getattr(args, "show", False):
                print(accounts.accounts[name])
            print(f"{args.command}: {name}; run render, then restart the proxies", file=sys.stderr)
        elif args.command == "list":
            for name in sorted(accounts.accounts):
                print(name)
        elif args.command == "raw-auth":
            if args.state is None:
                print("on" if accounts.raw_auth else "off")
            else:
                accounts.raw_auth = args.state == "on"
                accounts.save()
                print(f"raw-auth: {args.state}; run render, then restart the lines and xray-ha together",
                      file=sys.stderr)
        elif args.command == "render":
            written = render(accounts)
            for path in written:
                print(path.relative_to(accounts.base_dir))
            if not written:
                print("no changes", file=sys.stderr)
    except (AccountsError, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
