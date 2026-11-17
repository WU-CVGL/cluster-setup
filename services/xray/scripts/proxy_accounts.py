#!/usr/bin/env python3
"""
Proxy accounts: one protected accounts file, from which every authenticated proxy config is rendered.

    proxy_accounts.py add NAME [--show]      new account with a random password
    proxy_accounts.py rotate NAME [--show]   new password for an account
    proxy_accounts.py remove NAME
    proxy_accounts.py disable NAME           keep the account, refuse it at the entries that require accounts
    proxy_accounts.py enable NAME
    proxy_accounts.py admin NAME on|off      admin accounts may use ha-private and the private proxies
    proxy_accounts.py list                   account names, admins marked, without passwords
    proxy_accounts.py status [--json]        accounts, proxies and their ports, raw auth and the last applied file hash
    proxy_accounts.py raw-auth [on|off]      show or set authentication on the public proxies (off by default)
    proxy_accounts.py render                 write the configs below from the accounts file

Proxies: xray/public-proxies.txt lists the public proxies. Every other proxy (xray/<name>/) is private: for admin
accounts only.

The HA entry xray-ha has two entries: ha-general (inbounds http_IN, socks_IN) over the pool-* outbounds, which
must all lead to public proxies, and ha-private (inbounds ha-private_http_IN, ha-private_socks_IN) over the pool-*
and private-* outbounds, every proxy.

render writes, relative to services/ (all gitignored, mode 0600, replaced atomically):
  xray-ha/config/config.json     xray-ha/config.example.json with every enabled account except svc-ha required
                                 on ha-general, and the enabled admins and svc-monitor on ha-private; the outbounds
                                 log in as svc-ha to every proxy that requires accounts
  blackbox/config/blackbox.yml   blackbox/blackbox.example.yml with svc-monitor in the proxy URLs of the HA
                                 entries and the private proxies, and of the public proxies with raw auth
  xray/*/config/config.json      private proxies: the enabled admins, svc-ha and svc-monitor, always. Public proxies:
                                 with raw auth, every enabled account; without it, inbounds that carry accounts
                                 back to no authentication, other files untouched

Account names are cluster usernames; names starting with svc- are service accounts:
svc-ha (the HA entry's login to the proxies), svc-monitor (the probes), svc-docker-<node> (a node's Docker daemon).
svc-ha and svc-monitor cannot be disabled or removed; service accounts are never admins. A disabled account is
left out of every rendered config: with raw auth off, the public proxies' own entries require no account, so a
disabled account can still use them.

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
APPLIED_FILE = Path("proxy-accounts/applied.json")  # written by the portal's applier after an apply
ACCOUNTS_VERSION = 3
HA_EXAMPLE = Path("xray-ha/config.example.json")
HA_CONFIG = Path("xray-ha/config/config.json")
BLACKBOX_EXAMPLE = Path("blackbox/blackbox.example.yml")
BLACKBOX_CONFIG = Path("blackbox/config/blackbox.yml")
RAW_CONFIG_GLOB = "xray/*/config/config.json"
PUBLIC_PROXIES_FILE = Path("xray/public-proxies.txt")
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


def public_proxies(base_dir: Path) -> Set[str]:
    """
    The public proxies, from xray/public-proxies.txt: one proxy name per line, '#' starts a comment. A proxy not
    listed, including one nobody classified, is private. A missing file is an error, not an empty list.
    """
    path = Path(base_dir) / PUBLIC_PROXIES_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise AccountsError(f"{PUBLIC_PROXIES_FILE} is missing: it lists the public proxies")
    names = set()  # type: Set[str]
    for number, line in enumerate(text.splitlines(), 1):
        name = line.split("#", 1)[0].strip()
        if not name:
            continue
        if not PROXY_NAME_RE.fullmatch(name):
            raise AccountsError(f"{PUBLIC_PROXIES_FILE}:{number}: invalid proxy name {name!r}")
        if name in names:
            raise AccountsError(f"{PUBLIC_PROXIES_FILE}:{number}: {name} is listed twice")
        names.add(name)
    return names


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
    "accounts": {name: {"password": str, "enabled": bool, "admin": bool}}}.
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
        accounts, disabled, admins = {}, set(), set()
        for name, entry in entries.items():
            validate_name(name)
            if version == 1 and isinstance(entry, str):
                accounts[name] = entry
            elif (keys is not None and isinstance(entry, dict) and set(entry) == keys
                  and isinstance(entry["password"], str) and isinstance(entry["enabled"], bool)
                  and isinstance(entry.get("admin", False), bool)):
                accounts[name] = entry["password"]
                if not entry["enabled"]:
                    disabled.add(name)
                if entry.get("admin", False):
                    admins.add(name)
            else:
                raise AccountsError(f"{ACCOUNTS_FILE}: unknown version {version!r} or entry format for {name!r}")
        for name in PROTECTED:
            if name in disabled:
                raise AccountsError(f"{ACCOUNTS_FILE}: {name} cannot be disabled")
        for name in admins:
            if kind(name) == "service":
                raise AccountsError(f"{ACCOUNTS_FILE}: service account {name} cannot be an admin")
        self.raw_auth = bool(data.get("raw_auth", False))
        self.accounts, self.disabled, self.admins = accounts, disabled, admins

    def text(self) -> str:
        entries = {name: {"password": pw, "enabled": name not in self.disabled, "admin": name in self.admins}
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

    def remove(self, name: str) -> None:
        self.require(name)
        if name in PROTECTED:
            raise AccountsError(f"{name} cannot be removed")
        del self.accounts[name]
        self.disabled.discard(name)
        self.admins.discard(name)

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

    def proxy_users(self, private: bool) -> Optional[List[Tuple[str, str]]]:
        """
        Who may log in to a proxy's own entry: on a private proxy the enabled admins, svc-ha and svc-monitor, always;
        on a public proxy every enabled account with raw auth, else nobody needs to (None).
        """
        if private:
            return sorted(self.admin_pairs() + [(name, self.accounts[name]) for name in PROTECTED])
        return self.pairs() if self.raw_auth else None


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
    ports = compose_ports(accounts.base_dir)
    return {
        "raw_auth": accounts.raw_auth,
        # The entries that refuse a disabled account: while raw auth is off, the public proxies' own entries
        # require no account, so they do not
        "disabled_refused_at": [GENERAL, HA_PRIVATE, "private-proxies"]
                               + (["public-proxies"] if accounts.raw_auth else []),
        "accounts": [{"name": name, "kind": kind(name), "enabled": accounts.enabled(name),
                      "admin": name in accounts.admins, "protected": name in PROTECTED}
                     for name in sorted(accounts.accounts)],
        "proxies": [{"name": name, "access": "public" if name in public else "private",
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


def render_ha(example: dict, accounts: Accounts, public: Set[str]) -> dict:
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
        if proxy not in public or accounts.raw_auth:  # the proxy requires accounts
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


def render_outputs(accounts: Accounts) -> List[Tuple[Path, str]]:
    """Render every config in memory; returns the (path, text) of those that differ from the files."""
    base = accounts.base_dir
    for name in (MONITOR, RELAY):  # svc-ha: the private proxies always require accounts
        if name not in accounts.accounts:
            raise AccountsError(f"service account {name} is missing: run 'add {name}' first")
    public = public_proxies(base)
    outputs = []  # type: List[Tuple[Path, str]]
    ha = render_ha(json.loads((base / HA_EXAMPLE).read_text(encoding="utf-8")), accounts, public)
    outputs.append((base / HA_CONFIG, json.dumps(ha, indent=4) + "\n"))
    outputs.append((base / BLACKBOX_CONFIG,
                    render_blackbox((base / BLACKBOX_EXAMPLE).read_text(encoding="utf-8"), accounts, public)))
    for path in sorted(base.glob(RAW_CONFIG_GLOB)):
        raw_users = accounts.proxy_users(private=path.parent.parent.name not in public)
        old = json.loads(path.read_text(encoding="utf-8"))
        if raw_users is None and not any("users" in (i.get("settings") or {}) for i in old.get("inbounds") or []
                                         if i.get("protocol") in ("http", "socks")):
            continue  # a public proxy, raw auth off and no accounts there: leave its config as it is
        new = json.loads(json.dumps(old))
        try:
            XrayConfigHandler.set_inbound_users(new, raw_users)
        except ValueError as e:
            raise AccountsError(f"{path.relative_to(base)}: {e}")
        if new != old:
            # The format create_xray_service.py writes
            outputs.append((path, json.dumps(new, indent=4, ensure_ascii=False)))
    return [(path, text) for path, text in outputs
            if not (path.exists() and path.read_text(encoding="utf-8") == text)]


def render(accounts: Accounts) -> List[Path]:
    """Render every config first, then write them; returns the files written."""
    written = []
    for path, text in render_outputs(accounts):
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
                what = f"admin {args.state}" if args.command == "admin" else args.command
                print(f"{what}: {name}{note}; run render, then restart the proxies whose configs it writes",
                      file=sys.stderr)
            elif args.command == "raw-auth":
                accounts.raw_auth = args.state == "on"
                accounts.save()
                print(f"raw-auth: {args.state}; run render, then restart the public proxies and xray-ha together",
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
            print(f"{name} admin" if name in accounts.admins else name)
    elif args.command == "raw-auth":
        print("on" if accounts.raw_auth else "off")
    elif args.json:
        print(json.dumps(status(accounts), indent=2))
    else:
        view = status(accounts)
        scope = ("every entry" if accounts.raw_auth else
                 "ha-general, ha-private and the private proxies; the public proxies' own entries still accept them "
                 "(raw auth is off)")
        print(f"raw auth: {'on' if accounts.raw_auth else 'off'}; disabled accounts are refused at {scope}")
        for entry in view["accounts"]:
            state = "enabled" if entry["enabled"] else "disabled"
            flags = ("  admin" if entry["admin"] else "") + ("  protected" if entry["protected"] else "")
            print(f"{entry['name']:<32} {entry['kind']:<8} {state}{flags}")
        for proxy in view["proxies"]:
            access = "public" if proxy["access"] == "public" else "private"
            ports = "  ".join(f"{protocol} {port}" for protocol, port in proxy["ports"].items() if port)
            print(f"proxy {proxy['name']:<40} {access:<12} {ports}".rstrip())
        applied = view["applied"] or {}
        print(f"accounts file sha256: {view['accounts_sha256']}")
        print(f"last applied sha256:  {applied.get('accounts_sha256')} {applied.get('applied_at') or ''}".rstrip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
