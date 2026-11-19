"""
My access: the signed-in user's proxy account and the entries it may use, from the applier's status (the accounts
file at the applier's last run, never a pending portal request). Every account may use ha-general and the public
proxies; admin accounts also ha-private and the private proxies; another account also the private proxies granted to
it, at their own entries (never ha-private). The open proxies' own entries never require an account; with raw auth
off, no public proxy's own entry does. The proxy admin flag is the account's own, not the Determined admin role.
"""

from django.conf import settings

from . import proxy_status

DEFAULT_PORTS = {"ha-general": {"http": 30889, "socks": 30089}, "ha-private": {"http": 31889, "socks": 31089}}
NO_PROXY = "localhost,127.0.0.1,cvgl.lab,harbor.cvgl.lab,192.168.233.0/24,10.0.1.64/27"
TEST_URL = "https://www.google.com/generate_204"


def _addresses(ports):
    result = []
    for network, host in settings.PROXY_ADDRESSES:
        item = {"network": network, "host": host}
        if ports.get("http"):
            item["http"] = f"{host}:{ports['http']}"
        if ports.get("socks"):
            item["socks"] = f"{host}:{ports['socks']}"
        result.append(item)
    return result


def _entry(entry_id, kind, public, ports, usable, requires_account, granted=False):
    return {"id": entry_id, "kind": kind, "access": "public" if public else "private", "ports": ports,
            "addresses": _addresses(ports), "usable": usable, "requires_account": requires_account,
            "granted": granted}


def templates(username, ports):
    """Client settings for an entry, with <password> in place of the password, for each network."""
    result = {}
    for network, host in settings.PROXY_ADDRESSES:
        http = f"http://{username}:<password>@{host}:{ports['http']}" if ports.get("http") else None
        socks = f"socks5h://{username}:<password>@{host}:{ports['socks']}" if ports.get("socks") else None
        items = []
        if http:
            items.append(("environment", "\n".join([
                f"export http_proxy={http}",
                "export https_proxy=$http_proxy HTTP_PROXY=$http_proxy HTTPS_PROXY=$http_proxy",
                f"export no_proxy={NO_PROXY} NO_PROXY={NO_PROXY}"])))
            items.append(("curl", f"curl -x {http} {TEST_URL} -w '%{{http_code}}\\n'"))
        if socks:
            items.append(("socks5h", f"curl -x {socks} {TEST_URL} -w '%{{http_code}}\\n'"))
        if http:
            items.append(("git", f"git config --global http.https://github.com/.proxy {http}"))
            items.append(("pip", f"pip install --proxy {http} <package>"))
        result[network] = [{"kind": kind, "text": text} for kind, text in items]
    return result


def build(username):
    status = proxy_status.load()
    view = {
        "username": username,
        "status_available": status.available,
        "status_stale": status.stale if status.available else None,
        "status_generated_at": status.generated_at.isoformat() if status.generated_at else None,
        "raw_auth": status.raw_auth if status.available else None,
        "open_proxies": status.open_proxies if status.available else [],
        "status_unapplied_changes": status.unapplied_changes if status.available else None,
        "account": None,
        "entries": [],
        "templates": None,
    }
    if not status.available:
        return view
    account = status.accounts.get(username)
    if account is not None:
        view["account"] = {"name": account.name, "enabled": account.enabled, "admin": account.admin,
                           # the private proxies granted to it; an admin account may use every private proxy anyway
                           "proxies": [] if account.admin else list(account.proxies)}
    enabled = account is not None and account.enabled
    admin = enabled and account.admin
    general_ports = {k: v or DEFAULT_PORTS["ha-general"][k] for k, v in status.entry_ports["ha-general"].items()}
    private_ports = {k: v or DEFAULT_PORTS["ha-private"][k] for k, v in status.entry_ports["ha-private"].items()}
    show_admin = account is not None and account.admin
    entries = [_entry("ha-general", "ha", True, general_ports, enabled, True)]
    if show_admin:
        entries.append(_entry("ha-private", "ha", False, private_ports, admin, True))
    for proxy in status.proxies:
        if proxy.public:
            # an open proxy's own entry, and without raw auth every public proxy's, takes no account: anyone may use it
            requires = proxy.requires_account(status.raw_auth)
            entries.append(_entry(proxy.name, "proxy", True, proxy.ports, enabled if requires else True, requires))
    granted = []
    for proxy in status.proxies:
        if not proxy.public and show_admin:
            entries.append(_entry(proxy.name, "proxy", False, proxy.ports, admin, True))
        elif not proxy.public and account is not None and proxy.name in account.proxies:
            # a private proxy granted to this account: its own address only, never the Private Auto-switching Proxy
            entries.append(_entry(proxy.name, "proxy", False, proxy.ports, enabled, True, granted=True))
            granted.append(proxy)
    view["entries"] = entries
    if enabled:
        view["templates"] = {"entry": "ha-general", "networks": templates(username, general_ports),
                             "proxies": [{"id": proxy.name, "networks": templates(username, proxy.ports)}
                                         for proxy in granted if proxy.ports.get("http") or proxy.ports.get("socks")]}
    return view
