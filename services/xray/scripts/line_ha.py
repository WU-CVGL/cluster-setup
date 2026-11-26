"""Render ordered, fixed-destination standby paths for individual VLESS lines."""
import copy
import ipaddress
import json
import re
from urllib.parse import urlsplit

CONFIG = "xray-ha/standby/config.json"
PREFIX = "line-standby/"
POOL = PREFIX + "pool/0"
RULE = PREFIX + "traffic"
METRICS = PREFIX + "metrics"
SOCKET = PREFIX + "socket"


def port(value):
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError("expected a TCP port")
    return value


def host(value):
    try:
        ipaddress.ip_address(value)
    except ValueError:
        if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]*", value):
            raise ValueError("expected a hostname or IP address")
    if not isinstance(value, str):
        raise ValueError("expected a host string")
    return value


def load(path):
    if not path.exists():
        return {"paths": [], "lines": {}}
    site = json.loads(path.read_text())
    try:
        url = urlsplit(site["probe_url"])
        if url.scheme != "https" or not url.hostname:
            raise ValueError("probe_url must be an HTTPS URL")
        used = {}
        for route in site["paths"]:
            name = route["name"]
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", name) or name in used:
                raise ValueError("expected unique path names")
            used[name] = set()
            host(route["address"])
            if "control" in route:
                ctrl = route["control"]
                host(ctrl["host"])
                host(ctrl["server_name"])
                used[name].add(port(ctrl["port"]))
            else:
                ipaddress.ip_address(route["listener"])
                if not route["sources"]:
                    raise ValueError("relay requires allowed source networks")
                for source in route["sources"]:
                    ipaddress.ip_network(source)
        for name, line in site["lines"].items():
            if not re.fullmatch(r"[a-zA-Z0-9_-][a-zA-Z0-9_.-]*", name) or not line["ports"]:
                raise ValueError("expected a named line with enrolled paths")
            host(line["host"])
            port(line["port"])
            for route, listener in line["ports"].items():
                if port(listener) in used[route]:
                    raise ValueError("duplicate listener on " + route)
                used[route].add(listener)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("invalid standby inventory: " + str(exc)) from exc
    return site


def configure(config, site, name):
    routing = config.get("routing", {})
    rules = routing.get("rules", [])
    managed = any(r.get("ruleTag") == RULE and r.get("balancerTag") == POOL for r in rules)
    if managed:
        rules[:] = [r for r in rules if not r.get("ruleTag", "").startswith(PREFIX)]
        for key in ("outbounds", "inbounds"):
            config[key] = [item for item in config[key] if not item.get("tag", "").startswith(PREFIX)]
        routing["balancers"] = [b for b in routing.get("balancers", []) if not b["tag"].startswith(PREFIX)]
        if not routing["balancers"]:
            del routing["balancers"]
        observed = config.get("observatory", {}).get("subjectSelector", [])
        if any(tag.startswith(PREFIX) for tag in observed):
            del config["observatory"]
        if config.get("metrics") == {"tag": METRICS}:
            del config["metrics"]
    line = site["lines"].get(name)
    if line is None:
        return
    original = config["outbounds"][0]
    primary = original.get("tag")
    peers = original.get("settings", {}).get("vnext", [])
    if (original.get("protocol") != "vless" or len(peers) != 1
            or (peers[0]["address"], peers[0]["port"]) != (line["host"], line["port"])):
        raise ValueError("standby destination differs from line peer")
    tags = [item.get("tag", "") for key in ("outbounds", "inbounds") for item in config[key]]
    if (not primary or PREFIX.startswith(primary) or any(tag.startswith(PREFIX) for tag in tags)
            or sum(o.get("tag", "").startswith(primary) for o in config["outbounds"]) != 1
            or any(key in config for key in ("observatory", "burstObservatory", "metrics"))
            or "balancers" in routing or "rules" not in routing
            or any(r.get("outboundTag") == primary or r.get("ruleTag", "").startswith(PREFIX) for r in rules)
            or "proxySettings" in original or "dialerProxy" in original.get("streamSettings", {}).get("sockopt", {})):
        raise ValueError("line uses incompatible routing, tags or transport")
    choices = [primary]
    for path in site["paths"]:
        key = path["name"]
        if key not in line["ports"]:
            continue
        # Trailing separator prevents Xray's prefix selectors matching another path.
        tag = PREFIX + "via/" + key + "/"
        link = PREFIX + "link/" + key
        mirror = copy.deepcopy(original)
        mirror["tag"] = tag
        mirror.setdefault("streamSettings", {}).setdefault("sockopt", {})["dialerProxy"] = link
        addr = path["address"]
        endpoint = ("[" + addr + "]" if ":" in addr else addr) + ":" + str(line["ports"][key])
        config["outbounds"].extend([mirror, {"tag": link, "protocol": "freedom", "settings": {"redirect": endpoint}}])
        choices.append(tag)
    config["observatory"] = {"subjectSelector": choices, "probeUrl": site["probe_url"],
                             "probeInterval": "15s", "enableConcurrency": True}
    pools = routing["balancers"] = []
    for i, choice in enumerate(choices[:-1]):
        pool = PREFIX + "pool/" + str(i)
        fallback = choices[-1]
        if i + 2 < len(choices):
            hop = PREFIX + "next/" + str(i)
            fallback = hop
            config["outbounds"].append({"tag": hop, "protocol": "loopback", "settings": {"inboundTag": hop}})
            rules.insert(0, {"type": "field", "ruleTag": hop, "inboundTag": [hop],
                             "balancerTag": PREFIX + "pool/" + str(i + 1)})
        pools.append({"tag": pool, "selector": [choice], "strategy": {"type": "leastPing"}, "fallbackTag": fallback})
    rules.append({"type": "field", "ruleTag": RULE, "network": "tcp,udp", "balancerTag": POOL})
    config["metrics"] = {"tag": METRICS}
    config["inbounds"].append({"tag": SOCKET, "listen": "/etc/xray/.line-monitor/status.sock,0666",
                               "protocol": "dokodemo-door", "settings": {"address": "127.0.0.1", "port": 80}})
    rules.insert(0, {"type": "field", "ruleTag": SOCKET, "inboundTag": [SOCKET], "outboundTag": METRICS})
