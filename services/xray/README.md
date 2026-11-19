# Xray services

## Folder Structure

- `<outbound server1>`

  - config

    - `config.json`

  - log

- `<outbound server2>`

  - config

    - `config.json`

  - log

- ...

The `config/config.json` files are gitignored; they only exist on the supplementary services VM.


## Tools

See [scripts/README.md](scripts/README.md) (in Chinese):

- `scripts/create_xray_service.py <share_url> <name> <http_port> <socks_port> --public|--private`: parses a
  vmess/vless/trojan/ss share URL, writes `xray/<name>/config/config.json`, and adds `xray-<name>` +
  `xray-<name>-exporter` to `docker-compose.yml` and the exporter target to `prometheus/prometheus.yml`. The class
  is required: `--public` adds the proxy to `public-proxies.txt`; `--private` leaves it unlisted, so its own
  entry accepts only admin accounts (and the accounts later given that proxy) from the start ([docs/00](../../docs/00_Network_Proxy.md#classify-a-new-proxy)).
  A public proxy requires every proxy account from the start with `proxy_accounts.py raw-auth on`, unless it is in
  `open-proxies.txt`. Afterwards run
  `docker compose up -d xray-<name> xray-<name>-exporter` and `docker compose kill -s SIGHUP prometheus`.
- `public-proxies.txt`: the public proxies. Every proxy not listed is private: its own entry accepts admin accounts
  and the accounts given that proxy (`proxy_accounts.py grant`), and only the HA entry `ha-private` reaches it.
- `open-proxies.txt`: the open proxies, public proxies whose own entries never require an account, also with
  `raw-auth on` ([docs/00](../../docs/00_Network_Proxy.md#open-proxies)). Each must be in `public-proxies.txt`:
  `render` refuses a private proxy there.
- `scripts/proxy_accounts.py add|rotate|remove|enable|disable|admin|grant|revoke|list|status|raw-auth|render`: keeps
  the proxy accounts in `services/proxy-accounts/accounts.json` (not in git) and renders every config that holds them:
  the HA entry's, the probes', the private proxies' inbounds (admin accounts and the accounts given that proxy,
  always) and, with `raw-auth on`, the public
  proxies' inbounds but the open proxies'
  ([docs/00](../../docs/00_Network_Proxy.md#accounts)). The portal's changes go through
  [`proxy-portal/applier`](../proxy-portal/applier/README.md).
- `scripts/vless_to_config.py`: prints a standalone config for one `vless://` URL. Add `--stats-api` when the
  output replaces an existing `xray/<name>/config/config.json` (the exporter needs the stats API below).


## SLA relays

The name describes the exit country. The `relay_usca5` suffix identifies the VLESS server.

| Service | SLA Reality port | SOCKS5 | HTTP |
| --- | ---: | ---: | ---: |
| `miyaip-jp-01_relay_usca5-bwh-sla-2tb` | 32792 | 51089 | 51889 |
| `miyaip-jp-02_relay_usca5-bwh-sla-2tb` | 32793 | 57089 | 57889 |
| `miyaip-us-01_relay_usca5-bwh-sla-2tb` | 32794 | 60089 | 60889 |

For each new service, use its SLA Reality share link with `vless_to_config.py --stats-api` to
write `xray/<service>/config/config.json` on the supplementary services VM. The compose entries
already exist, so run `proxy_accounts.py render` before starting the services to apply private
proxy authentication. The existing JP 01 client can keep using port 32792.


## Config

> Reference: https://xtls.github.io/en/config/
>
> Official client/server examples for each protocol and transport (VLESS-REALITY, XTLS-Vision, WS, gRPC, VMess,
> Trojan, Shadowsocks, ...): https://github.com/XTLS/Xray-examples

In order to use the v2ray-dashboard, you need to enable the statistics in the config.json. Make sure you have `stats`, `api`, `inbound` for API and `routing` for API:

```jsonc

{
    "stats": {},
    "api": {
        "tag": "api",
        "services": [
            "StatsService"
        ]
    },
    "dns": {
      // "servers": []
    },
    // "policy"
    "inbounds": [
        // {http_IN}
        // {socks_IN}
        {
            "tag": "api",
            "port": 10085,
            "listen": "0.0.0.0",
            "protocol": "dokodemo-door",
            "settings": {
                "udp": false,
                "address": "0.0.0.0",
                "allowTransparent": false
            }
        }
    ],
    // "outbounds": [],
    "routing": {
        "domainStrategy": "AsIs",
        "domainMatcher": "mph",
        "rules": [
            {
                "inboundTag": [
                    "api"
                ],
                "outboundTag": "api",
                "type": "field",
                "enabled": true
            },
            {
                "ip": [
                    "geoip:private"
                ],
                "outboundTag": "DIRECT",
                "type": "field"
            },
            {
                "ip": [
                    "geoip:cn"
                ],
                "outboundTag": "DIRECT",
                "type": "field"
            },
            {
                "domain": [
                    "geosite:cn"
                ],
                "outboundTag": "DIRECT",
                "type": "field"
            }
        ]
    }

}

```
