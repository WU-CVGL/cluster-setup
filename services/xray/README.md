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


## Config

> Reference: https://xtls.github.io/en/config/

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
