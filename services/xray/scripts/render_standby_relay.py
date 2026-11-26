"""Generate one fixed-destination relay config from the line inventory."""
import argparse
import json
from pathlib import Path

import line_ha


def render(site, name, role):
    route = next((r for r in site["paths"] if r["name"] == name), None)
    if route is None:
        raise ValueError("unknown path")
    lines = [(n, l, l["ports"][name]) for n, l in site["lines"].items() if name in l["ports"]]
    if not lines:
        raise ValueError("path has no enrolled lines")
    if role == "xray":
        if "control" in route:
            raise ValueError("reverse path requires frps or frpc")
        inbounds = [{"tag": n, "listen": route["listener"], "port": port, "protocol": "dokodemo-door",
                     "settings": {"network": "tcp", "address": line["host"], "port": line["port"]}}
                    for n, line, port in lines]
        config = {"inbounds": inbounds, "outbounds": [{"tag": "deny", "protocol": "blackhole"},
                  {"tag": "peer", "protocol": "freedom"}], "routing": {"rules": [
                      {"type": "field", "inboundTag": [n for n, _, _ in lines], "source": route["sources"],
                       "outboundTag": "peer"}]}}
        return json.dumps(config, indent=2)
    ctrl = route.get("control")
    if not ctrl:
        raise ValueError("forward path requires xray")
    common = ['auth.method = "token"', 'auth.tokenSource.type = "file"',
              'auth.tokenSource.file.path = "/tls/token"', 'transport.tls.trustedCaFile = "/tls/ca.crt"']
    leaf = "server" if role == "frps" else "client"
    common.extend([f'transport.tls.certFile = "/tls/{leaf}.crt"', f'transport.tls.keyFile = "/tls/{leaf}.key"'])
    if role == "frps":
        ports = ", ".join("{ single = %d }" % p for _, _, p in lines)
        return "\n".join(common + [f'bindPort = {ctrl["port"]}', f'allowPorts = [{ports}]',
                                   f'maxPortsPerClient = {len(lines)}', 'transport.tls.force = true'])
    rows = common + [f'serverAddr = {json.dumps(ctrl["host"])}', f'serverPort = {ctrl["port"]}',
                     'loginFailExit = false', 'transport.tls.enable = true',
                     f'transport.tls.serverName = {json.dumps(ctrl["server_name"])}']
    for n, line, port in lines:
        rows.extend(["", "[[proxies]]", f"name = {json.dumps(n)}", 'type = "tcp"',
                     f'localIP = {json.dumps(line["host"])}', f'localPort = {line["port"]}', f'remotePort = {port}'])
    return "\n".join(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    parser.add_argument("role", choices=("xray", "frps", "frpc"))
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[2] / line_ha.CONFIG)
    args = parser.parse_args()
    try:
        print(render(line_ha.load(args.config), args.path, args.role))
    except ValueError as exc:
        parser.error(str(exc))
