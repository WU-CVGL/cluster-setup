#!/usr/bin/env python3
"""
Proxy accounts: one protected accounts file, from which every authenticated proxy config is rendered.

    proxy_accounts.py add NAME [--show]      new account with a random password
    proxy_accounts.py rotate NAME [--show]   new password for an account
    proxy_accounts.py remove NAME
    proxy_accounts.py disable NAME           keep the account, refuse it at the entries that require accounts
    proxy_accounts.py enable NAME
    proxy_accounts.py list                   account names, without passwords
    proxy_accounts.py status [--json]        names, kinds, enabled, raw auth and the last applied file hash
    proxy_accounts.py raw-auth [on|off]      show or set authentication on the proxies (off by default)
    proxy_accounts.py render                 write the configs below from the accounts file

render writes, relative to services/ (all gitignored, mode 0600, replaced atomically):
  xray-ha/config/config.json     xray-ha/config.example.json with every enabled account except svc-ha required
                                 on both inbounds; with raw auth, the pool outbounds log in to the proxies as svc-ha
  blackbox/config/blackbox.yml   blackbox/blackbox.example.yml with svc-monitor in the proxy URLs of the HA
                                 entry, and of the proxies with raw auth
  xray/*/config/config.json      with raw auth, every enabled account required on the proxies' inbounds; without
                                 it, inbounds that carry accounts back to no authentication, other files untouched

Account names are cluster usernames; names starting with svc- are service accounts:
svc-ha (the HA entry's login to the proxies), svc-monitor (the probes), svc-docker-<node> (a node's Docker daemon).
svc-ha and svc-monitor cannot be disabled or removed. A disabled account is left out of every rendered config:
with raw auth off, only the HA entry requires accounts, so only the HA entry refuses it.

The commands that change the accounts file or render hold an exclusive lock (proxy-accounts/.lock), which the
portal's applier (proxy-portal/applier/proxy_apply.py) holds for a whole apply.
"""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import urllib.parse
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from XrayConfigHandler import XrayConfigHandler  # noqa: E402

SERVICES_DIR = Path(__file__).resolve().parents[2]
ACCOUNTS_FILE = Path("proxy-accounts/accounts.json")
LOCK_FILE = Path("proxy-accounts/.lock")
APPLIED_FILE = Path("proxy-accounts/applied.json")  # written by the portal's applier after an apply, removed by render
ACCOUNTS_VERSION = 2
HA_EXAMPLE = Path("xray-ha/config.example.json")
HA_CONFIG = Path("xray-ha/config/config.json")
BLACKBOX_EXAMPLE = Path("blackbox/blackbox.example.yml")
BLACKBOX_CONFIG = Path("blackbox/config/blackbox.yml")
RAW_CONFIG_GLOB = "xray/*/config/config.json"

RELAY = "svc-ha"
MONITOR = "svc-monitor"
PROTECTED = (RELAY, MONITOR)
POOL_PREFIX = "pool-"
HA_HOST = "xray-ha"

NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
SERVICE_RE = re.compile(r"svc-(?:ha|monitor|docker-[a-z0-9][a-z0-9._-]*)")
AUTH_KEYS = {"users", "accounts"}  # an inbound's logins: "accounts" is the older name, removed on render
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


def _keep_owner(fd: int, path: Path) -> None:
    """Run as root, give fd the owner of the file it replaces, or of its folder: the admins keep their files."""
    if os.geteuid() != 0:
        return
    try:
        st = os.lstat(str(path))
    except FileNotFoundError:
        st = os.lstat(str(path.parent))
    os.fchown(fd, st.st_uid, st.st_gid)


def write_private(path: Path, text: str) -> None:
    """Write text to path with mode 0600, replacing the file atomically (same directory, then rename)."""
    if not path.parent.exists():
        path.parent.mkdir(mode=0o700, parents=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        _keep_owner(fd, path)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def accounts_lock(base_dir: Path) -> Iterator[None]:
    """Hold the exclusive lock on the accounts file (proxy-accounts/.lock), waiting for it if needed."""
    path = Path(base_dir) / LOCK_FILE
    if not path.parent.exists():
        path.parent.mkdir(mode=0o700, parents=True)
    fd = os.open(str(path), os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if os.geteuid() == 0 and os.fstat(fd).st_uid == 0:
            st = os.lstat(str(path.parent))
            os.fchown(fd, st.st_uid, st.st_gid)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"waiting for {LOCK_FILE}", file=sys.stderr)
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Accounts:
    """
    The accounts file, version 2: {"version": 2, "raw_auth": bool,
    "accounts": {name: {"password": str, "enabled": bool}}}.
    A file without "version" is version 1, {"raw_auth": bool, "accounts": {name: password}}: it is read with
    every account enabled and written back as version 2.
    """

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.path = self.base_dir / ACCOUNTS_FILE
        self.raw_auth = False
        self.accounts = {}  # type: Dict[str, str]
        self.disabled = set()  # type: Set[str]
        self.loaded_text = None  # type: Optional[str]
        if self.path.exists():
            self.loaded_text = self.path.read_text(encoding="utf-8")
            self.load(self.loaded_text)

    def load(self, text: str) -> None:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise AccountsError(f"{ACCOUNTS_FILE}: not a JSON object")
        version = data.get("version", 1)
        entries = data.get("accounts") or {}
        if not isinstance(entries, dict):
            raise AccountsError(f"{ACCOUNTS_FILE}: accounts is not an object")
        accounts, disabled = {}, set()
        for name, entry in entries.items():
            validate_name(name)
            if version == 1 and isinstance(entry, str):
                accounts[name] = entry
            elif (version == ACCOUNTS_VERSION and isinstance(entry, dict) and isinstance(entry.get("password"), str)
                  and isinstance(entry.get("enabled"), bool)):
                accounts[name] = entry["password"]
                if not entry["enabled"]:
                    disabled.add(name)
            else:
                raise AccountsError(f"{ACCOUNTS_FILE}: unknown version {version!r} or entry format for {name!r}")
        for name in PROTECTED:
            if name in disabled:
                raise AccountsError(f"{ACCOUNTS_FILE}: {name} cannot be disabled")
        self.raw_auth = bool(data.get("raw_auth", False))
        self.accounts, self.disabled = accounts, disabled

    def text(self) -> str:
        entries = {name: {"password": pw, "enabled": name not in self.disabled}
                   for name, pw in sorted(self.accounts.items())}
        data = {"version": ACCOUNTS_VERSION, "raw_auth": self.raw_auth, "accounts": entries}
        return json.dumps(data, indent=2) + "\n"

    def save(self) -> None:
        write_private(self.path, self.text())

    def require(self, name: str) -> None:
        if name not in self.accounts:
            raise AccountsError(f"no account {name!r}")

    def enabled(self, name: str) -> bool:
        return name in self.accounts and name not in self.disabled

    def set_enabled(self, name: str, enabled: bool) -> bool:
        """Enable or disable an account; returns whether anything changed."""
        self.require(name)
        if not enabled and name in PROTECTED:
            raise AccountsError(f"{name} cannot be disabled")
        if self.enabled(name) == enabled:
            return False
        if enabled:
            self.disabled.discard(name)
        else:
            self.disabled.add(name)
        return True

    def remove(self, name: str) -> None:
        self.require(name)
        if name in PROTECTED:
            raise AccountsError(f"{name} cannot be removed")
        del self.accounts[name]
        self.disabled.discard(name)

    def pairs(self, exclude: Tuple[str, ...] = ()) -> List[Tuple[str, str]]:
        """The enabled accounts, without those in exclude."""
        return [(name, pw) for name, pw in sorted(self.accounts.items())
                if name not in exclude and name not in self.disabled]


def kind(name: str) -> str:
    return "service" if name.startswith("svc-") else "user"


def status(accounts: Accounts) -> dict:
    """A view of the accounts without passwords, for the portal (status --json)."""
    applied = None
    try:
        applied = json.loads((accounts.base_dir / APPLIED_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    except ValueError:
        applied = {"error": f"{APPLIED_FILE} is not valid JSON"}
    current = sha256_text(accounts.loaded_text) if accounts.loaded_text is not None else None
    applied_sha = applied.get("accounts_sha256") if isinstance(applied, dict) else None
    return {
        "raw_auth": accounts.raw_auth,
        # Where a disabled account is refused: only the HA entry while the proxies require no accounts
        "disabled_refused_at": "ha-entry-and-proxies" if accounts.raw_auth else "ha-entry",
        "accounts": [{"name": name, "kind": kind(name), "enabled": accounts.enabled(name),
                      "protected": name in PROTECTED} for name in sorted(accounts.accounts)],
        "accounts_sha256": current,
        "applied": applied,
        "unapplied_changes": None if applied_sha is None else current != applied_sha,
    }


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


def render_files(accounts: Accounts) -> List[Tuple[Path, str]]:
    """
    Render every config in memory: the (path, text) of each file that render keeps, changed or not. A proxy
    config that needs no change keeps its text. One left alone (raw auth off, no logins) is listed too: it may
    still run with logins, so the applier's full apply restarts it.
    """
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
        old_text = path.read_text(encoding="utf-8")
        old = json.loads(old_text)
        if raw_users is None and not any(AUTH_KEYS & set(i.get("settings") or {}) for i in old.get("inbounds") or []
                                         if i.get("protocol") in ("http", "socks")):
            outputs.append((path, old_text))
            continue  # raw auth off and no accounts there: leave the proxy's config as it is
        new = json.loads(json.dumps(old))
        try:
            XrayConfigHandler.set_inbound_users(new, raw_users)
        except ValueError as e:
            raise AccountsError(f"{path.relative_to(base)}: {e}")
        # The format create_xray_service.py writes
        outputs.append((path, json.dumps(new, indent=4, ensure_ascii=False) if new != old else old_text))
    return outputs


def render_outputs(accounts: Accounts) -> List[Tuple[Path, str]]:
    """Render every config in memory; returns the (path, text) of those that differ from the files."""
    return [(path, text) for path, text in render_files(accounts)
            if not (path.exists() and path.read_text(encoding="utf-8") == text)]


def render_sha256(base_dir: Path, files: List[Tuple[Path, str]]) -> str:
    """One hash over the rendered files' paths and texts (render_files), as the applier records it."""
    lines = sorted(f"{path.relative_to(base_dir).as_posix()}\0{sha256_text(text)}\n" for path, text in files)
    return sha256_text("".join(lines))


def forget_applied(base_dir: Path) -> None:
    """Remove applied.json: what runs is no longer known to match a recorded apply."""
    try:
        (Path(base_dir) / APPLIED_FILE).unlink()
    except FileNotFoundError:
        pass


def render(accounts: Accounts) -> List[Path]:
    """
    Render every config first, then write them; returns the files written. Before it writes any file it removes
    applied.json: the files then differ from what the applier last applied, and a restart by hand is not recorded,
    so the next apply restarts and verifies every service.
    """
    outputs = render_outputs(accounts)
    if outputs:
        forget_applied(accounts.base_dir)
    written = []
    for path, text in outputs:
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
    for command in ("remove", "enable", "disable"):
        sub.add_parser(command).add_argument("name")
    sub.add_parser("list")
    sub.add_parser("status").add_argument("--json", action="store_true", help="machine-readable, for the portal")
    sub.add_parser("raw-auth").add_argument("state", nargs="?", choices=("on", "off"))
    sub.add_parser("render")
    args = parser.parse_args(argv)

    try:
        if args.command in ("list", "status") or (args.command == "raw-auth" and args.state is None):
            return read_command(Accounts(args.base_dir), args)
        with accounts_lock(args.base_dir):
            accounts = Accounts(args.base_dir)
            if args.command in ("add", "rotate", "remove", "enable", "disable"):
                name = validate_name(args.name)
                if args.command == "add" and name in accounts.accounts:
                    raise AccountsError(f"account {name!r} exists; use rotate")
                if args.command == "remove":
                    accounts.remove(name)
                elif args.command in ("enable", "disable"):
                    accounts.set_enabled(name, args.command == "enable")
                else:
                    if args.command == "rotate":
                        accounts.require(name)
                    accounts.accounts[name] = new_password()
                accounts.save()
                if getattr(args, "show", False):
                    print(accounts.accounts[name])
                note = ""
                if args.command == "disable" and not accounts.raw_auth:
                    note = " (raw auth is off: only the HA entry refuses it)"
                print(f"{args.command}: {name}{note}; run render, then restart the proxies", file=sys.stderr)
            elif args.command == "raw-auth":
                accounts.raw_auth = args.state == "on"
                accounts.save()
                print(f"raw-auth: {args.state}; run render, then restart the proxies and xray-ha together",
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


def read_command(accounts: Accounts, args: argparse.Namespace) -> int:
    if args.command == "list":
        for name in sorted(accounts.accounts):
            print(name)
    elif args.command == "raw-auth":
        print("on" if accounts.raw_auth else "off")
    elif args.json:
        print(json.dumps(status(accounts), indent=2))
    else:
        view = status(accounts)
        scope = "the HA entry and the proxies" if accounts.raw_auth else "the HA entry only (raw auth is off)"
        print(f"raw auth: {'on' if accounts.raw_auth else 'off'}; disabled accounts are refused at {scope}")
        for entry in view["accounts"]:
            state = "enabled" if entry["enabled"] else "disabled"
            print(f"{entry['name']:<32} {entry['kind']:<8} {state}{'  protected' if entry['protected'] else ''}")
        applied = view["applied"] or {}
        print(f"accounts file sha256: {view['accounts_sha256']}")
        last = f"{applied.get('accounts_sha256') or 'unknown'} {applied.get('applied_at') or ''}".rstrip()
        print(f"last applied sha256:  {last}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
