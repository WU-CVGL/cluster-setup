#!/usr/bin/env python3
"""
Proxy accounts: one protected accounts file, from which every authenticated proxy config is rendered.

    proxy_accounts.py add NAME [--show]      new account with a random password
    proxy_accounts.py rotate NAME [--show]   new password for an account
    proxy_accounts.py remove NAME
    proxy_accounts.py disable NAME           keep the account, refuse it at the entries that require accounts
    proxy_accounts.py enable NAME
    proxy_accounts.py admin NAME on|off      admin accounts may use ha-private and the private proxies
    proxy_accounts.py grant NAME PROXY       NAME may also use the private proxy PROXY at its own entry
    proxy_accounts.py revoke NAME PROXY      take that back
    proxy_accounts.py list                   account names, admins and granted private proxies marked, no passwords
    proxy_accounts.py status [--json]        accounts, proxies and their ports, raw auth and the last applied file hash
    proxy_accounts.py raw-auth [on|off]      show or set authentication on the public proxies (off by default)
    proxy_accounts.py render                 write the configs below from the accounts file

Proxies: xray/public-proxies.txt lists the public proxies. Every other proxy (xray/<name>/) is private: for admin
accounts, and for the accounts granted that proxy (grant), at its own entry only. xray/open-proxies.txt lists the
open proxies, public proxies whose own entries never require an account, also with raw auth; a private proxy is never
open.

The HA entry xray-ha has two entries: ha-general (inbounds http_IN, socks_IN) over the pool-* outbounds, which
must all lead to public proxies, and ha-private (inbounds ha-private_http_IN, ha-private_socks_IN) over the pool-*
and private-* outbounds, every proxy.

render writes, relative to services/ (all gitignored, mode 0600, replaced atomically):
  xray-ha/config/config.json     xray-ha/config.example.json with every enabled account except svc-ha required
                                 on ha-general, and the enabled admins and svc-monitor on ha-private; the outbounds
                                 log in as svc-ha to every proxy that requires accounts
  blackbox/config/blackbox.yml   blackbox/blackbox.example.yml with svc-monitor in the proxy URLs of the HA
                                 entries and the private proxies, and of the public proxies with raw auth
  xray/*/config/config.json      private proxies: the enabled admins, the enabled accounts granted that proxy, svc-ha
                                 and svc-monitor, always. Public proxies:
                                 with raw auth, every enabled account; without it, and on the open proxies always,
                                 inbounds that carry accounts back to no authentication, other files untouched

Account names are cluster usernames; names starting with svc- are service accounts:
svc-ha (the HA entry's login to the proxies), svc-monitor (the probes), svc-docker-<node> (a node's Docker daemon).
svc-ha and svc-monitor cannot be disabled or removed; service accounts are never admins and are never granted a
proxy. Only a private proxy that has a config can be granted, and never to an admin account, which may use every
private proxy already; an account keeps its grants when it becomes an admin, and they count again once it is no
longer one. A grant to a proxy that is public or has no config any more stops render until it is revoked. Grants never
change ha-private or ha-general. A disabled account is
left out of every rendered config: the open proxies' own entries, and with raw auth off every public proxy's own
entry, require no account, so a disabled account can still use them.

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
ACCOUNTS_VERSION = 3
HA_EXAMPLE = Path("xray-ha/config.example.json")
HA_CONFIG = Path("xray-ha/config/config.json")
BLACKBOX_EXAMPLE = Path("blackbox/blackbox.example.yml")
BLACKBOX_CONFIG = Path("blackbox/config/blackbox.yml")
RAW_CONFIG_GLOB = "xray/*/config/config.json"
PUBLIC_PROXIES_FILE = Path("xray/public-proxies.txt")
OPEN_PROXIES_FILE = Path("xray/open-proxies.txt")
COMPOSE_FILE = Path("docker-compose.yml")

RELAY = "svc-ha"
MONITOR = "svc-monitor"
PROTECTED = (RELAY, MONITOR)
POOL_PREFIX = "pool-"          # outbounds to public proxies: ha-general and ha-private
PRIVATE_PREFIX = "private-"    # outbounds to private proxies: ha-private only
GENERAL = "ha-general"
HA_PRIVATE = "ha-private"
GENERAL_INBOUNDS = ("http_IN", "socks_IN")
HA_PRIVATE_INBOUNDS = ("ha-private_http_IN", "ha-private_socks_IN")
API_TAG = "api"  # the stats API's inbound, routed to Xray's API handler
API_PROTOCOLS = ("dokodemo-door", "tunnel")
HA_TOP_LEVEL = {"log", "stats", "api", "policy", "inbounds", "outbounds", "observatory", "routing"}
HA_HOST = "xray-ha"
# Container ports of the proxy entries, as in docker-compose.yml: a proxy's HTTP and SOCKS5 inbounds, and the HA
# entries' (ha-general on a proxy's ports, ha-private on its own)
PROXY_CONTAINER_PORTS = {"http": 8889, "socks": 1089}
HA_CONTAINER_PORTS = {GENERAL: {"http": 8889, "socks": 1089}, HA_PRIVATE: {"http": 8890, "socks": 1090}}
PROXY_HOST_PREFIX = "xray-"

NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
PROXY_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")  # create_xray_service.py's service names
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


def _proxy_list(path: Path, rel: Path) -> Set[str]:
    """One proxy name per line, '#' starts a comment."""
    text = path.read_text(encoding="utf-8")
    names = set()  # type: Set[str]
    for number, line in enumerate(text.splitlines(), 1):
        name = line.split("#", 1)[0].strip()
        if not name:
            continue
        if not PROXY_NAME_RE.fullmatch(name):
            raise AccountsError(f"{rel}:{number}: invalid proxy name {name!r}")
        if name in names:
            raise AccountsError(f"{rel}:{number}: {name} is listed twice")
        names.add(name)
    return names


def public_proxies(base_dir: Path) -> Set[str]:
    """
    The public proxies, from xray/public-proxies.txt: one proxy name per line, '#' starts a comment. A proxy not
    listed, including one nobody classified, is private. A missing file is an error, not an empty list.
    """
    try:
        return _proxy_list(Path(base_dir) / PUBLIC_PROXIES_FILE, PUBLIC_PROXIES_FILE)
    except FileNotFoundError:
        raise AccountsError(f"{PUBLIC_PROXIES_FILE} is missing: it lists the public proxies")


def open_proxies(base_dir: Path, public: Set[str]) -> Set[str]:
    """
    The open proxies, from xray/open-proxies.txt: public proxies whose own entries never require an account, also
    with raw auth. A private proxy (one not in public) in the list is an error: it always requires accounts. Without
    the file no proxy is open, which only ever requires more accounts.
    """
    try:
        names = _proxy_list(Path(base_dir) / OPEN_PROXIES_FILE, OPEN_PROXIES_FILE)
    except FileNotFoundError:
        return set()
    private = sorted(names - public)
    if private:
        raise AccountsError(f"{OPEN_PROXIES_FILE}: {', '.join(private)} not in {PUBLIC_PROXIES_FILE}: "
                            "a private proxy always requires accounts and can never be open")
    return names


def requires_accounts(proxy: str, public: Set[str], open_: Set[str], raw_auth: bool) -> bool:
    """Whether a proxy's own entry requires accounts: a private proxy always, an open one never, another public
    proxy with raw auth."""
    if proxy not in public:
        return True
    return raw_auth and proxy not in open_


def proxy_names(base_dir: Path) -> List[str]:
    """The proxies that have a config: xray/<name>/config/config.json."""
    return sorted(path.parent.parent.name for path in Path(base_dir).glob(RAW_CONFIG_GLOB))


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
    The accounts file, version 3: {"version": 3, "raw_auth": bool,
    "accounts": {name: {"password": str, "enabled": bool, "admin": bool[, "proxies": [private proxy, ...]]}}}.
    "proxies", the private proxies granted to the account, is optional and written only when it is not empty: a file
    without grants is the same as before grants existed.
    Older files are read and written back as version 3 at the next change: version 2 ({"password", "enabled"}
    entries) with no admins, and a file without "version", version 1 ({name: password}), with every account
    enabled and no admins.
    """

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.path = self.base_dir / ACCOUNTS_FILE
        self.raw_auth = False
        self.accounts = {}  # type: Dict[str, str]
        self.disabled = set()  # type: Set[str]
        self.admins = set()  # type: Set[str]
        self.grants = {}  # type: Dict[str, Set[str]]  # name -> granted private proxies, never empty
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
        keys = {2: {"password", "enabled"}, 3: {"password", "enabled", "admin"}}.get(version)
        optional = {"proxies"} if version == 3 else set()
        accounts, disabled, admins, grants = {}, set(), set(), {}
        for name, entry in entries.items():
            validate_name(name)
            if version == 1 and isinstance(entry, str):
                accounts[name] = entry
            elif (keys is not None and isinstance(entry, dict) and keys <= set(entry) <= keys | optional
                  and isinstance(entry["password"], str) and isinstance(entry["enabled"], bool)
                  and isinstance(entry.get("admin", False), bool)):
                accounts[name] = entry["password"]
                if not entry["enabled"]:
                    disabled.add(name)
                if entry.get("admin", False):
                    admins.add(name)
                if "proxies" in entry:
                    proxies = entry["proxies"]
                    if not isinstance(proxies, list) or not all(isinstance(p, str) and PROXY_NAME_RE.fullmatch(p)
                                                                for p in proxies) \
                            or len(set(proxies)) != len(proxies):
                        raise AccountsError(f"{ACCOUNTS_FILE}: proxies of {name!r} must be a list of distinct "
                                            "proxy names")
                    if proxies:
                        grants[name] = set(proxies)
            else:
                raise AccountsError(f"{ACCOUNTS_FILE}: unknown version {version!r} or entry format for {name!r}")
        for name in PROTECTED:
            if name in disabled:
                raise AccountsError(f"{ACCOUNTS_FILE}: {name} cannot be disabled")
        for name in admins:
            if kind(name) == "service":
                raise AccountsError(f"{ACCOUNTS_FILE}: service account {name} cannot be an admin")
        for name in grants:
            if kind(name) == "service":
                raise AccountsError(f"{ACCOUNTS_FILE}: service account {name} cannot be granted a proxy")
        self.raw_auth = bool(data.get("raw_auth", False))
        self.accounts, self.disabled, self.admins, self.grants = accounts, disabled, admins, grants

    def text(self) -> str:
        entries = {}
        for name, pw in sorted(self.accounts.items()):
            entries[name] = {"password": pw, "enabled": name not in self.disabled, "admin": name in self.admins}
            if self.grants.get(name):
                entries[name]["proxies"] = sorted(self.grants[name])
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

    def set_admin(self, name: str, admin: bool) -> bool:
        """Make an account an admin or not; returns whether anything changed."""
        self.require(name)
        if admin and kind(name) == "service":
            raise AccountsError(f"{name} is a service account and cannot be an admin")
        if (name in self.admins) == admin:
            return False
        if admin:
            self.admins.add(name)
        else:
            self.admins.discard(name)
        return True

    def is_admin(self, name: str) -> bool:
        """An enabled admin: may use ha-private and the private proxies."""
        return self.enabled(name) and name in self.admins

    def granted(self, name: str) -> List[str]:
        """The private proxies granted to an account, sorted; [] when none."""
        return sorted(self.grants.get(name, ()))

    def grant(self, name: str, proxy: str, private: Set[str]) -> bool:
        """
        Grant an account a private proxy, one of private (the private proxies that have a config); returns whether
        anything changed. Refused for a service account, an admin account and any other proxy name.
        """
        self.require(name)
        if kind(name) == "service":
            raise AccountsError(f"{name} is a service account and cannot be granted a proxy")
        if name in self.admins:
            raise AccountsError(f"{name} is an admin account and may use every private proxy already")
        if not PROXY_NAME_RE.fullmatch(proxy) or proxy not in private:
            raise AccountsError(f"{proxy!r} is not a private proxy: only a proxy that has a config (xray/<name>/) "
                                f"and is not in {PUBLIC_PROXIES_FILE} can be granted")
        if proxy in self.grants.get(name, ()):
            return False
        self.grants.setdefault(name, set()).add(proxy)
        return True

    def revoke(self, name: str, proxy: str) -> bool:
        """Take a granted proxy back, whatever it is now; returns whether anything changed."""
        self.require(name)
        if proxy not in self.grants.get(name, ()):
            return False
        self.grants[name].discard(proxy)
        if not self.grants[name]:
            del self.grants[name]
        return True

    def remove(self, name: str) -> None:
        self.require(name)
        if name in PROTECTED:
            raise AccountsError(f"{name} cannot be removed")
        del self.accounts[name]
        self.disabled.discard(name)
        self.admins.discard(name)
        self.grants.pop(name, None)

    def pairs(self, exclude: Tuple[str, ...] = ()) -> List[Tuple[str, str]]:
        """The enabled accounts, without those in exclude."""
        return [(name, pw) for name, pw in sorted(self.accounts.items())
                if name not in exclude and name not in self.disabled]

    def admin_pairs(self) -> List[Tuple[str, str]]:
        """The enabled admins."""
        return [(name, pw) for name, pw in sorted(self.accounts.items()) if self.is_admin(name)]

    def ha_private_users(self) -> List[Tuple[str, str]]:
        """Who may log in to ha-private: the enabled admins and svc-monitor."""
        return sorted(self.admin_pairs() + [(MONITOR, self.accounts[MONITOR])])

    def grantee_pairs(self, proxy: str) -> List[Tuple[str, str]]:
        """The enabled accounts granted proxy that are not admins (the admins are there already)."""
        return [(name, pw) for name, pw in sorted(self.accounts.items())
                if proxy in self.grants.get(name, ()) and self.enabled(name) and not self.is_admin(name)]

    def proxy_users(self, private: bool, open_: bool = False,
                    proxy: Optional[str] = None) -> Optional[List[Tuple[str, str]]]:
        """
        Who may log in to a proxy's own entry: on a private proxy the enabled admins, the enabled accounts granted
        that proxy, svc-ha and svc-monitor, always; on an open proxy nobody needs to (None), whatever raw auth says;
        on another public proxy every enabled account with raw auth, else nobody needs to.
        """
        if private:
            granted = self.grantee_pairs(proxy) if proxy is not None else []
            return sorted(self.admin_pairs() + granted + [(name, self.accounts[name]) for name in PROTECTED])
        return self.pairs() if self.raw_auth and not open_ else None


def kind(name: str) -> str:
    return "service" if name.startswith("svc-") else "user"


def _host_port(entry) -> Tuple[Optional[int], Optional[int]]:
    """(container port, host port) of a compose ports entry: "59889:8889", "127.0.0.1:30889:8889" or the long form."""
    if isinstance(entry, dict):
        target, published = entry.get("target"), entry.get("published")
    else:
        text = str(entry).strip().strip("\"'").split("/", 1)[0]
        if text.startswith("["):
            text = text.partition("]")[2].lstrip(":")
        parts = text.split(":")
        if len(parts) not in (2, 3):
            return None, None
        published, target = parts[-2], parts[-1]
    try:
        return int(str(target)), int(str(published))
    except (TypeError, ValueError):
        return None, None


def compose_ports(base_dir: Path) -> Dict[str, Dict[int, int]]:
    """{service: {container port: host port}} from docker-compose.yml; {} when it is missing or unreadable."""
    try:
        text = (Path(base_dir) / COMPOSE_FILE).read_text(encoding="utf-8")
        from create_xray_service import compose_services  # it imports this module
        services = compose_services(text)
    except Exception as e:  # ports are informational: status must never fail on them
        print(f"warning: no ports from {COMPOSE_FILE}: {type(e).__name__}", file=sys.stderr)
        return {}
    result = {}  # type: Dict[str, Dict[int, int]]
    for service, entries in services.items():
        if not isinstance(entries, list):
            continue
        ports = {}  # type: Dict[int, int]
        for entry in entries:
            target, published = _host_port(entry)
            if target is not None:
                ports.setdefault(target, published)
        result[service] = ports
    return result


def _ports(published: Dict[int, int], container: Dict[str, int]) -> Dict[str, Optional[int]]:
    return {protocol: published.get(port) for protocol, port in container.items()}


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
    proxies = proxy_names(accounts.base_dir)
    public = public_proxies(accounts.base_dir) if proxies else set()
    open_ = open_proxies(accounts.base_dir, public) if proxies else set()
    ports = compose_ports(accounts.base_dir)
    return {
        "raw_auth": accounts.raw_auth,
        # The entries that refuse a disabled account: while raw auth is off, the public proxies' own entries
        # require no account, so they do not; with it, the public proxies other than the open ones do
        "disabled_refused_at": [GENERAL, HA_PRIVATE, "private-proxies"]
                               + (["public-proxies"] if accounts.raw_auth else []),
        "accounts": [{"name": name, "kind": kind(name), "enabled": accounts.enabled(name),
                      "admin": name in accounts.admins, "protected": name in PROTECTED,
                      # the private proxies granted to the account, usable at their own entries while it is enabled
                      "proxies": accounts.granted(name)}
                     for name in sorted(accounts.accounts)],
        "proxies": [{"name": name, "access": "public" if name in public else "private",
                     # an open proxy's own entry never requires an account
                     "open": name in open_,
                     # host ports from docker-compose.yml, null when not published
                     "ports": _ports(ports.get(PROXY_HOST_PREFIX + name, {}), PROXY_CONTAINER_PORTS)}
                    for name in proxies],
        "entries": {entry: {"ports": _ports(ports.get(HA_HOST, {}), container)}
                    for entry, container in HA_CONTAINER_PORTS.items()},
        "accounts_sha256": current,
        "applied": applied,
        "unapplied_changes": None if applied_sha is None else current != applied_sha,
    }


def _proxy_of(outbound: dict) -> str:
    """The proxy a pool-* or private-* outbound leads to: one SOCKS server xray-<proxy>:1089."""
    servers = (outbound.get("settings") or {}).get("servers") or []
    address = servers[0].get("address", "") if len(servers) == 1 and isinstance(servers[0], dict) else ""
    if outbound.get("protocol") != "socks" or not address.startswith(PROXY_HOST_PREFIX) or address == HA_HOST:
        raise AccountsError(f"{HA_EXAMPLE}: outbound {outbound.get('tag')!r} must be SOCKS to one xray-<proxy>")
    return address[len(PROXY_HOST_PREFIX):]


def check_ha(config: dict, public: Set[str]) -> Dict[str, str]:
    """
    Check that no path of the HA example lets ha-general reach a private proxy; returns {outbound tag: proxy}.
    The first outbound (Xray's default) is the blackhole; every outbound is the blackhole, pool-* to a public
    proxy or private-* to a private proxy, with no other keys (no proxySettings or streamSettings to chain it);
    ha-general selects only pool-* outbounds and ha-private every proxy; the inbounds are exactly the four entry
    inbounds (ha-general_* and ha-private_*) and the API's dokodemo-door; each entry inbound is routed, by its tag
    alone, to its own entry's balancer, and the API inbound only to the API; the observatory covers every proxy.
    """
    def fail(message):
        raise AccountsError(f"{HA_EXAMPLE}: {message}")

    extra = set(config) - HA_TOP_LEVEL
    if extra:  # e.g. reverse, whose portals are outbounds no check below sees
        fail(f"unexpected top-level keys {', '.join(sorted(extra))}")
    if (config.get("api") or {}).get("tag") != API_TAG:
        fail(f"the API must be tagged {API_TAG}")
    outbounds = config.get("outbounds") or []
    if not outbounds or outbounds[0].get("protocol") != "blackhole" or outbounds[0].get("tag") != "blackhole":
        fail("the first outbound must be the blackhole, tagged blackhole")
    if set(outbounds[0]) - {"protocol", "tag", "settings"}:
        fail("the blackhole may set only protocol, tag and settings")
    proxies = {}  # type: Dict[str, str]
    for outbound in outbounds[1:]:
        tag = str(outbound.get("tag", ""))
        if tag.startswith(POOL_PREFIX) or tag.startswith(PRIVATE_PREFIX):
            proxy = _proxy_of(outbound)
            if set(outbound) - {"protocol", "tag", "settings"} or set(outbound["settings"]) != {"servers"} \
                    or set(outbound["settings"]["servers"][0]) - {"address", "port"}:
                fail(f"outbound {tag} may set only protocol, tag and settings.servers[0].address and port")
            if tag.startswith(POOL_PREFIX) and proxy not in public:
                fail(f"{tag} leads to {proxy}, which is not in {PUBLIC_PROXIES_FILE}: "
                     f"private proxies take {PRIVATE_PREFIX}*")
            if tag.startswith(PRIVATE_PREFIX) and proxy in public:
                fail(f"{tag} leads to the public proxy {proxy}: public proxies take {POOL_PREFIX}*")
            proxies[tag] = proxy
        else:
            fail(f"unexpected outbound {tag!r}: only the blackhole, {POOL_PREFIX}* and {PRIVATE_PREFIX}*")
    if not any(t.startswith(POOL_PREFIX) for t in proxies):
        fail(f"no {POOL_PREFIX}* outbounds")

    entry_inbounds = GENERAL_INBOUNDS + HA_PRIVATE_INBOUNDS
    inbounds = config.get("inbounds") or []
    if sorted((str(i.get("tag")) for i in inbounds)) != sorted(entry_inbounds + (API_TAG,)):
        fail(f"the inbounds must be exactly {', '.join(entry_inbounds)} and {API_TAG}")
    for inbound in inbounds:
        tag, protocol = inbound["tag"], inbound.get("protocol")
        want = API_PROTOCOLS if tag == API_TAG else ("http",) if tag.endswith("http_IN") else ("socks",)
        if protocol not in want:
            fail(f"inbound {tag} must be {' or '.join(want)}, not {protocol!r}")

    routing = config.get("routing") or {}
    balancers = {b.get("tag"): b for b in routing.get("balancers") or []}
    if set(balancers) != {GENERAL, HA_PRIVATE}:
        fail(f"the balancers must be exactly {GENERAL} and {HA_PRIVATE}")
    for name, balancer in balancers.items():
        if balancer.get("fallbackTag") != "blackhole":
            fail(f"{name} must fall back to the blackhole")
        selected = {t for t in proxies if any(t.startswith(str(s)) for s in balancer.get("selector") or [])}
        if name == GENERAL and (not selected or any(not t.startswith(POOL_PREFIX) for t in selected)):
            fail(f"{GENERAL} must select only {POOL_PREFIX}* outbounds")
        if name == HA_PRIVATE and selected != set(proxies):
            fail(f"{HA_PRIVATE} must select every proxy: missing {', '.join(sorted(set(proxies) - selected))}")
    observed = {t for t in proxies if any(t.startswith(str(s)) for s in
                                         (config.get("observatory") or {}).get("subjectSelector") or [])}
    if observed != set(proxies):
        fail(f"the observatory must cover every proxy: missing {', '.join(sorted(set(proxies) - observed))}")

    routed = {}  # type: Dict[str, str]
    for rule in routing.get("rules") or []:
        inbound_tags = rule.get("inboundTag")
        if not inbound_tags:
            fail("every routing rule must name its inbounds (inboundTag)")
        for tag in inbound_tags:
            if tag in routed:
                fail(f"inbound {tag} is in more than one rule")
            routed[tag] = rule.get("balancerTag") or rule.get("outboundTag") or ""
        entry_tags = set(inbound_tags) & set(entry_inbounds)
        if entry_tags:
            tags = set(inbound_tags)
            want = (GENERAL if tags <= set(GENERAL_INBOUNDS)
                    else HA_PRIVATE if tags <= set(HA_PRIVATE_INBOUNDS) else None)
            if want is None or set(rule) - {"type", "ruleTag"} != {"inboundTag", "balancerTag"} \
                    or rule["balancerTag"] != want:
                fail(f"the rule for {', '.join(sorted(tags))} must send them, by inboundTag only, "
                     f"to the balancer {want or 'of their own entry'}")
        elif rule != {"type": "field", "inboundTag": [API_TAG], "outboundTag": API_TAG}:
            fail(f"the only rule for inbounds other than the entry inbounds must send {API_TAG}, "
                 f"by inboundTag only, to the outbound {API_TAG}")
    missing = [t for t in entry_inbounds if t not in routed]
    if missing:
        fail(f"no routing rule for {', '.join(missing)}")
    return proxies


def render_ha(example: dict, accounts: Accounts, public: Set[str], open_: Set[str]) -> dict:
    config = json.loads(json.dumps(example))
    proxies = check_ha(config, public)
    try:
        XrayConfigHandler.set_inbound_users(config, accounts.pairs(exclude=(RELAY,)), tags=GENERAL_INBOUNDS)
        XrayConfigHandler.set_inbound_users(config, accounts.ha_private_users(), tags=HA_PRIVATE_INBOUNDS)
    except ValueError as e:
        raise AccountsError(f"{HA_EXAMPLE}: {e}")
    for outbound in config["outbounds"]:
        proxy = proxies.get(outbound.get("tag"))
        if proxy is None:
            continue
        server = outbound["settings"]["servers"][0]
        if requires_accounts(proxy, public, open_, accounts.raw_auth):
            server["users"] = [{"user": RELAY, "pass": accounts.accounts[RELAY]}]
        else:
            server.pop("users", None)
    return config


def render_blackbox(example: str, accounts: Accounts, public: Set[str]) -> str:
    userinfo = (urllib.parse.quote(MONITOR, safe="") + ":"
                + urllib.parse.quote(accounts.accounts[MONITOR], safe="") + "@")
    lines, ha_urls = [], 0
    for line in example.splitlines(True):
        match = PROXY_URL_RE.match(line.rstrip("\n"))
        if match:
            prefix, scheme, host, rest = match.groups()
            ha_urls += host == HA_HOST
            proxy = host[len(PROXY_HOST_PREFIX):] if host.startswith(PROXY_HOST_PREFIX) and host != HA_HOST else None
            auth = host == HA_HOST or (proxy is not None and (proxy not in public or accounts.raw_auth))
            line = f"{prefix}{scheme}://{userinfo if auth else ''}{host}{rest}\n"
        elif "proxy_url:" in line:
            raise AccountsError(f"{BLACKBOX_EXAMPLE}: cannot parse {line.strip()!r}")
        lines.append(line)
    if not ha_urls:
        raise AccountsError(f"{BLACKBOX_EXAMPLE} has no proxy_url through {HA_HOST}")
    header = (f"# Rendered by xray/scripts/proxy_accounts.py from {BLACKBOX_EXAMPLE.as_posix()}; "
              "holds credentials, not in git.\n")
    return header + "".join(lines)


def has_logins(config: dict) -> bool:
    """Whether a proxy config's HTTP or SOCKS5 inbound holds logins."""
    return any(AUTH_KEYS & set(i.get("settings") or {}) for i in config.get("inbounds") or []
               if i.get("protocol") in ("http", "socks"))


def private_proxies(base_dir: Path, public: Optional[Set[str]] = None) -> Set[str]:
    """The private proxies: those with a config (xray/<name>/config/config.json) not in public-proxies.txt."""
    public = public_proxies(base_dir) if public is None else public
    return set(proxy_names(base_dir)) - public


def check_grants(accounts: Accounts, private: Set[str]) -> None:
    """Every granted proxy must be a private proxy with a config; else the grant must be revoked first."""
    for name in sorted(accounts.grants):
        stale = sorted(accounts.grants[name] - private)
        if stale:
            raise AccountsError(f"{name} is granted {', '.join(stale)}, which is not a private proxy (public, or no "
                                f"config): revoke it with 'revoke {name} {stale[0]}' first")


def render_files(accounts: Accounts) -> List[Tuple[Path, str]]:
    """
    Render every config in memory: the (path, text) of each file that render keeps, changed or not. A proxy
    config that needs no change keeps its text. One left alone (raw auth off, no logins) is listed too: it may
    still run with logins, so the applier's full apply restarts it.
    """
    base = accounts.base_dir
    for name in (MONITOR, RELAY):  # svc-ha: the private proxies always require accounts
        if name not in accounts.accounts:
            raise AccountsError(f"service account {name} is missing: run 'add {name}' first")
    public = public_proxies(base)
    open_ = open_proxies(base, public)
    check_grants(accounts, private_proxies(base, public))
    outputs = []  # type: List[Tuple[Path, str]]
    ha = render_ha(json.loads((base / HA_EXAMPLE).read_text(encoding="utf-8")), accounts, public, open_)
    outputs.append((base / HA_CONFIG, json.dumps(ha, indent=4) + "\n"))
    outputs.append((base / BLACKBOX_CONFIG,
                    render_blackbox((base / BLACKBOX_EXAMPLE).read_text(encoding="utf-8"), accounts, public)))
    for path in sorted(base.glob(RAW_CONFIG_GLOB)):
        name = path.parent.parent.name
        raw_users = accounts.proxy_users(private=name not in public, open_=name in open_, proxy=name)
        old_text = path.read_text(encoding="utf-8")
        old = json.loads(old_text)
        if raw_users is None and not has_logins(old):
            outputs.append((path, old_text))
            continue  # a public proxy that requires no account, with no accounts there: leave its config as it is
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
    """
    One hash over the rendered files' paths and texts (render_files), as the applier records it. A proxy config
    without logins is left out: it holds no account state, so adding, editing or removing one leaves the hash as
    it is.
    """
    lines = []
    for path, text in files:
        rel = path.relative_to(base_dir)
        if rel.match(RAW_CONFIG_GLOB) and not has_logins(json.loads(text)):
            continue
        lines.append(f"{rel.as_posix()}\0{sha256_text(text)}\n")
    return sha256_text("".join(sorted(lines)))


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
    p = sub.add_parser("admin")
    p.add_argument("name")
    p.add_argument("state", choices=("on", "off"))
    for command in ("grant", "revoke"):
        p = sub.add_parser(command)
        p.add_argument("name")
        p.add_argument("proxy", help="a private proxy (xray/<proxy>/)")
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
            if args.command in ("add", "rotate", "remove", "enable", "disable", "admin"):
                name = validate_name(args.name)
                # read before anything is saved: a bad list fails the command with nothing changed
                open_ = open_proxies(accounts.base_dir, public_proxies(accounts.base_dir)) \
                    if args.command == "disable" and accounts.raw_auth else set()
                if args.command == "add" and name in accounts.accounts:
                    raise AccountsError(f"account {name!r} exists; use rotate")
                if args.command == "remove":
                    accounts.remove(name)
                elif args.command in ("enable", "disable"):
                    accounts.set_enabled(name, args.command == "enable")
                elif args.command == "admin":
                    accounts.set_admin(name, args.state == "on")
                else:
                    if args.command == "rotate":
                        accounts.require(name)
                    accounts.accounts[name] = new_password()
                accounts.save()
                if getattr(args, "show", False):
                    print(accounts.accounts[name])
                note = ""
                if args.command == "disable" and not accounts.raw_auth:
                    note = " (raw auth is off: the public proxies' own entries still accept it)"
                elif args.command == "disable" and open_:
                    note = " (the open proxies' own entries still accept it)"
                what = f"admin {args.state}" if args.command == "admin" else args.command
                print(f"{what}: {name}{note}; run render, then restart the proxies whose configs it writes",
                      file=sys.stderr)
            elif args.command in ("grant", "revoke"):
                name = validate_name(args.name)
                if args.command == "grant":
                    changed = accounts.grant(name, args.proxy, private_proxies(accounts.base_dir))
                else:
                    changed = accounts.revoke(name, args.proxy)
                if not changed:
                    print(f"{args.command}: {name} {args.proxy}: no change", file=sys.stderr)
                    return 0
                accounts.save()
                if accounts.enabled(name) and name not in accounts.admins:
                    then = f"run render, then restart {PROXY_HOST_PREFIX + args.proxy}"
                else:  # its logins there do not change while it is disabled or an admin
                    then = "run render; no proxy config changes while the account is " + \
                        ("disabled" if not accounts.enabled(name) else "an admin")
                print(f"{args.command}: {name} {args.proxy}; {then}", file=sys.stderr)
            elif args.command == "raw-auth":
                accounts.raw_auth = args.state == "on"
                accounts.save()
                print(f"raw-auth: {args.state}; run render, then restart the public proxies other than the open ones "
                      "and xray-ha together", file=sys.stderr)
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
            granted = accounts.granted(name)
            print(name + (" admin" if name in accounts.admins else "")
                  + (f" proxies={','.join(granted)}" if granted else ""))
    elif args.command == "raw-auth":
        print("on" if accounts.raw_auth else "off")
    elif args.json:
        print(json.dumps(status(accounts), indent=2))
    else:
        view = status(accounts)
        open_ = sorted(p["name"] for p in view["proxies"] if p["open"])
        scope = ("every entry" + (f" but the open proxies' own entries ({', '.join(open_)})" if open_ else "")
                 if accounts.raw_auth else
                 "ha-general, ha-private and the private proxies; the public proxies' own entries still accept them "
                 "(raw auth is off)")
        print(f"raw auth: {'on' if accounts.raw_auth else 'off'}; disabled accounts are refused at {scope}")
        for entry in view["accounts"]:
            state = "enabled" if entry["enabled"] else "disabled"
            flags = ("  admin" if entry["admin"] else "") + ("  protected" if entry["protected"] else "") \
                + (f"  proxies {','.join(entry['proxies'])}" if entry["proxies"] else "")
            print(f"{entry['name']:<32} {entry['kind']:<8} {state}{flags}")
        for proxy in view["proxies"]:
            access = "private" if proxy["access"] != "public" else "public open" if proxy["open"] else "public"
            ports = "  ".join(f"{protocol} {port}" for protocol, port in proxy["ports"].items() if port)
            print(f"proxy {proxy['name']:<40} {access:<12} {ports}".rstrip())
        applied = view["applied"] or {}
        print(f"accounts file sha256: {view['accounts_sha256']}")
        last = f"{applied.get('accounts_sha256') or 'unknown'} {applied.get('applied_at') or ''}".rstrip()
        print(f"last applied sha256:  {last}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
