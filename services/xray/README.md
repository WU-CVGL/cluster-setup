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

- `scripts/create_xray_service.py <share_url> <name> <http_port> <socks_port>`: parses a vmess/vless/trojan/ss
  share URL, writes `xray/<name>/config/config.json`, and adds `xray-<name>` + `xray-<name>-exporter` to
  `docker-compose.yml` and the exporter target to `prometheus/prometheus.yml`. With `proxy_accounts.py raw-auth on`,
  the new line requires every proxy account from the start. Afterwards run
  `docker compose up -d xray-<name> xray-<name>-exporter` and `docker compose kill -s SIGHUP prometheus`.
- `scripts/proxy_accounts.py add|rotate|remove|list|raw-auth|render`: keeps the proxy accounts in
  `services/proxy-accounts/accounts.json` (not in git) and renders every config that holds them: the HA entry's,
  the probes' and, with `raw-auth on`, the inbounds of every `xray/<name>/config/config.json`
  ([docs/00](../../docs/00_Network_Proxy.md#accounts)).
- `scripts/vless_to_config.py`: prints a standalone config for one `vless://` URL. Add `--stats-api` when the
  output replaces an existing `xray/<name>/config/config.json` (the exporter needs the stats API below).


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
