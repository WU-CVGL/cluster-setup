#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XrayConfigHandler - 解析 Xray 分享 URL 并生成配置
参考: https://github.com/2dust/v2rayN/blob/master/v2rayN/ServiceLib/Handler/ConfigHandler.cs
"""

import base64
import json
import urllib.parse
from typing import Any, Dict, Iterable, Optional, Tuple


DEFAULT_HTTP_PORT = 8889
DEFAULT_SOCKS_PORT = 1089
DEFAULT_API_PORT = 10085

# build_xray_config 自己使用的 tag。分享链接的 remark 与之相同时加后缀，
# 否则 DIRECT/BLACKHOLE 会让 Xray 启动失败，api 会把代理流量送进统计 API。
RESERVED_OUTBOUND_TAGS = ("api", "DIRECT", "BLACKHOLE")
RESERVED_TAG_SUFFIX = "-proxy"

# 各协议当前支持的网络/TLS 选项。覆盖范围与重构前的三个转换函数保持一致
# (例如 fingerprint 和 reality 字段只对 vless 生效，trojan 只接受 tls)。
_STREAM_PROFILES = {
    'vmess': {
        'network_keys': ('net', 'network'),
        'security_keys': ('tls', 'security'),
        'default_security': '',
        'security_modes': ('tls', 'xtls', 'reality'),
        'fingerprint': False,
        'reality': False,
        'transports': ('ws', 'http', 'grpc', 'quic'),
        'grpc_service_keys': ('path',),
    },
    'vless': {
        'network_keys': ('type', 'network'),
        'security_keys': ('security', 'tls'),
        'default_security': '',
        'security_modes': ('tls', 'xtls', 'reality'),
        'fingerprint': True,
        'reality': True,
        'transports': ('ws', 'http', 'grpc'),
        'grpc_service_keys': ('serviceName', 'path'),
    },
    'trojan': {
        'network_keys': ('type', 'network'),
        'security_keys': ('security', 'tls'),
        'default_security': 'tls',
        'security_modes': ('tls',),
        'fingerprint': False,
        'reality': False,
        'transports': ('ws', 'grpc'),
        'grpc_service_keys': ('serviceName', 'path'),
    },
}


def _require_port(value: Any, protocol: str) -> int:
    """端口必须是 1-65535 的整数，缺失或无效时给出明确的错误"""
    if value is None or str(value).strip() == '':
        raise ValueError(f"{protocol} 分享链接缺少端口")
    try:
        port = int(value)
    except (TypeError, ValueError):
        port = None
    if port is None or not 1 <= port <= 65535:
        raise ValueError(f"{protocol} 分享链接端口无效: {value!r} (应为 1-65535)")
    return port


def _url_port(parsed: urllib.parse.ParseResult, protocol: str) -> int:
    """从 host:port 中取端口；urllib 对非数字或越界端口会抛 ValueError"""
    try:
        port = parsed.port
    except ValueError:
        port_text = parsed.netloc.rpartition('@')[2].rpartition(':')[2]
        raise ValueError(f"{protocol} 分享链接端口无效: {port_text!r} (应为 1-65535)") from None
    return _require_port(port, protocol)


def _userinfo(parsed: urllib.parse.ParseResult) -> Optional[str]:
    """
    返回 percent-decode 后的完整 userinfo (最后一个 '@' 之前的部分)。
    不用 parsed.username：它不解码，而且会在未编码的 ':' 处截断。
    """
    userinfo, sep, _ = parsed.netloc.rpartition('@')
    if not sep:
        return None
    return urllib.parse.unquote(userinfo)


def _remark(parsed: urllib.parse.ParseResult) -> str:
    return urllib.parse.unquote(parsed.fragment) if parsed.fragment else ''


def _merge_query(config: Dict[str, Any], query: str) -> Dict[str, Any]:
    """
    把查询参数合并进 config (单值取字符串，多值保留列表)。
    查询参数不能覆盖已从 URL 本身解析出的核心字段 (address、port、id/password、protocol、remark 等)。
    """
    params = urllib.parse.parse_qs(query)
    for key, value in params.items():
        if key in config:
            continue
        if len(value) == 1:
            config[key] = value[0]
        else:
            config[key] = value
    return config


def _b64decode_text(encoded: str) -> str:
    """base64 / base64url 解码 (自动补齐 '=')"""
    return base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)).decode('utf-8')


def _split_host_port(server: str) -> Tuple[str, Optional[str]]:
    """拆分 host:port；没有端口时 port 为 None"""
    if ':' not in server or server.endswith(']'):
        return server, None
    host, _, port = server.rpartition(':')
    return host, port


def _first_present(config: Dict[str, Any], keys: Iterable[str], default: Any) -> Any:
    """等价于 config.get(k1, config.get(k2, default))：取第一个存在的键 (值可以为空)"""
    for key in keys:
        if key in config:
            return config[key]
    return default


def _first_truthy(config: Dict[str, Any], keys: Iterable[str]) -> Any:
    """等价于 config.get(k1) or config.get(k2)"""
    for key in keys:
        if config.get(key):
            return config.get(key)
    return None


def _split_alpn(alpn: Any) -> Any:
    return alpn.split(',') if isinstance(alpn, str) else alpn


def _security_settings(config: Dict[str, Any], security: str, profile: Dict[str, Any]) -> Dict[str, Any]:
    """tlsSettings / xtlsSettings / realitySettings"""
    settings = {}
    server_name = config.get('sni') or config.get('host')
    if server_name:
        settings["serverName"] = server_name
    if config.get('alpn'):
        settings["alpn"] = _split_alpn(config.get('alpn'))
    if profile['fingerprint'] and config.get('fp'):
        settings["fingerprint"] = config.get('fp')
    if profile['reality'] and security == 'reality':
        for src, dst in (('pbk', 'publicKey'), ('sid', 'shortId'), ('spx', 'spiderX')):
            if config.get(src):
                settings[dst] = config.get(src)
    return settings


def _transport_settings(config: Dict[str, Any], network: Any,
                        profile: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """网络类型特定设置，返回 (键名, 设置)；该协议不支持的网络类型或没有参数时返回 None"""
    if network not in profile['transports']:
        return None

    settings = {}
    if network == 'ws':
        key = "wsSettings"
        if config.get('path'):
            settings["path"] = config.get('path')
        if config.get('host'):
            settings["headers"] = {"Host": config.get('host')}
    elif network == 'http':
        key = "httpSettings"
        if config.get('path'):
            settings["path"] = config.get('path')
        if config.get('host'):
            settings["host"] = [config.get('host')]
    elif network == 'grpc':
        key = "grpcSettings"
        service_name = _first_truthy(config, profile['grpc_service_keys'])
        if service_name:
            settings["serviceName"] = service_name
    else:  # quic
        key = "quicSettings"
        if config.get('type'):
            settings["security"] = config.get('type')
        if config.get('key'):
            settings["key"] = config.get('key')
        if config.get('path'):
            settings["path"] = config.get('path')

    return (key, settings) if settings else None


def _stream_settings(config: Dict[str, Any], protocol: str) -> Dict[str, Any]:
    """vmess / vless / trojan 共用的 streamSettings 生成 (网络类型、TLS/XTLS/Reality、传输设置)"""
    profile = _STREAM_PROFILES[protocol]

    # 处理流设置
    network = _first_present(config, profile['network_keys'], 'tcp')
    stream = {"network": network}

    # TLS/XTLS/Reality
    security = _first_present(config, profile['security_keys'], profile['default_security'])
    if security in profile['security_modes']:
        stream["security"] = security
        security_settings = _security_settings(config, security, profile)
        if security_settings:
            stream[f"{security}Settings"] = security_settings

    # 网络类型特定设置
    transport = _transport_settings(config, network, profile)
    if transport:
        stream[transport[0]] = transport[1]

    return stream


def _outbound_tag(remark: Any) -> Any:
    """remark 作为 outbound tag；与保留 tag (api/DIRECT/BLACKHOLE) 相同时加后缀"""
    if remark in RESERVED_OUTBOUND_TAGS:
        return f"{remark}{RESERVED_TAG_SUFFIX}"
    return remark


def _outbound(protocol: str, settings: Dict[str, Any], stream_settings: Dict[str, Any], remark: Any) -> Dict[str, Any]:
    return {
        "protocol": protocol,
        "sendThrough": "0.0.0.0",
        "settings": settings,
        "streamSettings": stream_settings,
        "tag": _outbound_tag(remark),
    }


def _parse_userinfo_url(url: str, protocol: str, credential_key: str) -> Dict[str, Any]:
    """解析 <protocol>://credential@host:port?params#remark 格式 (vless、trojan)"""
    parsed = urllib.parse.urlparse(url)
    config = {
        credential_key: _userinfo(parsed),
        'address': parsed.hostname,
        'port': _url_port(parsed, protocol),
        'protocol': protocol,
        'remark': _remark(parsed),
    }
    # 解析查询参数
    return _merge_query(config, parsed.query)


class XrayConfigHandler:
    """解析 Xray 分享 URL 并转换为 Xray 配置格式"""

    @staticmethod
    def parse_vmess_url(url: str) -> Dict[str, Any]:
        """
        解析 vmess:// URL
        格式: vmess://base64(json)
        """
        try:
            # 移除协议头
            encoded = url.replace('vmess://', '')
            # Base64 解码
            decoded = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4))
            config = json.loads(decoded.decode('utf-8'))
            # 确保 protocol 字段存在
            config['protocol'] = 'vmess'
            return config
        except Exception as e:
            raise ValueError(f"解析 vmess URL 失败: {e}")

    @staticmethod
    def parse_vless_url(url: str) -> Dict[str, Any]:
        """
        解析 vless:// URL
        格式: vless://uuid@host:port?params#remark
        """
        try:
            return _parse_userinfo_url(url, 'vless', 'id')
        except Exception as e:
            raise ValueError(f"解析 vless URL 失败: {e}")

    @staticmethod
    def parse_trojan_url(url: str) -> Dict[str, Any]:
        """
        解析 trojan:// URL
        格式: trojan://password@host:port?params#remark
        """
        try:
            return _parse_userinfo_url(url, 'trojan', 'password')
        except Exception as e:
            raise ValueError(f"解析 trojan URL 失败: {e}")

    @staticmethod
    def parse_shadowsocks_url(url: str) -> Dict[str, Any]:
        """
        解析 ss:// URL
        格式: ss://base64(method:password)@host:port#remark
        或: ss://method:password@host:port#remark (SIP002 明文 userinfo，percent-encode，如 SS-2022)
        或: ss://base64(method:password@host:port)#remark
        """
        try:
            parsed = urllib.parse.urlparse(url)

            # 处理两种格式
            if '@' in parsed.netloc:
                # 格式: ss://userinfo@host:port
                auth_part, _, server_part = parsed.netloc.rpartition('@')
                auth = urllib.parse.unquote(auth_part)
                # base64 字母表里没有 ':'，含 ':' 的就是明文 method:password
                if ':' not in auth:
                    auth = _b64decode_text(auth)
                method, password = auth.split(':', 1)
            else:
                # 格式: ss://base64(method:password@host:port)
                decoded = _b64decode_text(parsed.netloc)
                # 密码中可能含 '@'，host 中不会，所以按最后一个 '@' 拆分
                auth, sep, server_part = decoded.rpartition('@')
                if not sep:
                    raise ValueError("无法解析 shadowsocks URL 格式")
                method, password = auth.split(':', 1)
            host, port = _split_host_port(server_part)

            config = {
                'method': method,
                'password': password,
                'address': host,
                'port': _require_port(port, 'shadowsocks'),
                'protocol': 'shadowsocks',
                'remark': _remark(parsed)
            }

            # 解析查询参数
            return _merge_query(config, parsed.query)
        except Exception as e:
            raise ValueError(f"解析 shadowsocks URL 失败: {e}")

    @staticmethod
    def parse_share_url(url: str) -> Dict[str, Any]:
        """
        自动识别并解析各种 Xray 分享 URL
        支持: vmess://, vless://, trojan://, ss://
        """
        url = url.strip()

        if url.startswith('vmess://'):
            return XrayConfigHandler.parse_vmess_url(url)
        elif url.startswith('vless://'):
            return XrayConfigHandler.parse_vless_url(url)
        elif url.startswith('trojan://'):
            return XrayConfigHandler.parse_trojan_url(url)
        elif url.startswith('ss://'):
            return XrayConfigHandler.parse_shadowsocks_url(url)
        else:
            raise ValueError(f"不支持的 URL 协议: {url[:20]}...")

    @staticmethod
    def vmess_to_xray_outbound(vmess_config: Dict[str, Any]) -> Dict[str, Any]:
        """将 vmess 配置转换为 Xray outbound 格式"""
        # 处理字段名称变体
        address = vmess_config.get('add') or vmess_config.get('address') or ''
        port = _require_port(vmess_config.get('port'), 'vmess')
        user_id = vmess_config.get('id') or vmess_config.get('uuid') or ''
        alter_id = int(vmess_config.get('aid') or vmess_config.get('alterId') or 0)
        security = vmess_config.get('scy') or vmess_config.get('security') or 'auto'
        remark = vmess_config.get('ps') or vmess_config.get('remark') or 'vmess'

        settings = {
            "vnext": [
                {
                    "address": address,
                    "port": port,
                    "users": [
                        {
                            "id": user_id,
                            "alterId": alter_id,
                            "security": security,
                            # 分享链接里的 v 是链接格式版本，不是用户等级；policy 只定义了 level 0
                            "level": 0
                        }
                    ]
                }
            ]
        }
        return _outbound("vmess", settings, _stream_settings(vmess_config, 'vmess'), remark)

    @staticmethod
    def vless_to_xray_outbound(vless_config: Dict[str, Any]) -> Dict[str, Any]:
        """将 vless 配置转换为 Xray outbound 格式"""
        settings = {
            "vnext": [
                {
                    "address": vless_config.get('address', ''),
                    "port": _require_port(vless_config.get('port'), 'vless'),
                    "users": [
                        {
                            "id": vless_config.get('id', ''),
                            "encryption": vless_config.get('encryption', 'none'),
                            "flow": vless_config.get('flow', '')
                        }
                    ]
                }
            ]
        }
        return _outbound("vless", settings, _stream_settings(vless_config, 'vless'),
                         vless_config.get('remark', 'vless'))

    @staticmethod
    def trojan_to_xray_outbound(trojan_config: Dict[str, Any]) -> Dict[str, Any]:
        """将 trojan 配置转换为 Xray outbound 格式"""
        settings = {
            "servers": [
                {
                    "address": trojan_config.get('address', ''),
                    "port": _require_port(trojan_config.get('port'), 'trojan'),
                    "password": trojan_config.get('password', ''),
                    "email": trojan_config.get('email', '')
                }
            ]
        }
        return _outbound("trojan", settings, _stream_settings(trojan_config, 'trojan'),
                         trojan_config.get('remark', 'trojan'))

    @staticmethod
    def shadowsocks_to_xray_outbound(ss_config: Dict[str, Any]) -> Dict[str, Any]:
        """将 shadowsocks 配置转换为 Xray outbound 格式"""
        settings = {
            "servers": [
                {
                    "address": ss_config.get('address', ''),
                    "port": _require_port(ss_config.get('port'), 'shadowsocks'),
                    "method": ss_config.get('method', ''),
                    "password": ss_config.get('password', ''),
                    "email": ss_config.get('email', '')
                }
            ]
        }
        return _outbound("shadowsocks", settings, {}, ss_config.get('remark', 'shadowsocks'))

    @staticmethod
    def build_xray_config(
        outbound: Dict[str, Any],
        http_port: int = DEFAULT_HTTP_PORT,
        socks_port: int = DEFAULT_SOCKS_PORT,
        loglevel: str = "warning",
        access_log: Optional[str] = None,
        error_log: Optional[str] = None,
        include_stats_api: bool = False,
        api_port: int = DEFAULT_API_PORT,
    ) -> Dict[str, Any]:
        """
        Build a complete Xray config from a single outbound definition.

        This is the only config template: create_xray_service.py uses it with
        include_stats_api=True, loglevel="none" and /var/log/xray log paths.
        Top-level key order (log, stats, api, dns, policy, inbounds, outbounds,
        routing) matches the files that tool has always written.
        """
        log_config = {
            "loglevel": loglevel,
        }
        if access_log:
            log_config["access"] = access_log
        if error_log:
            log_config["error"] = error_log

        config = {
            "log": log_config,
        }

        if include_stats_api:
            config["stats"] = {}
            config["api"] = {
                "tag": "api",
                "services": [
                    "StatsService",
                ],
            }

        config["dns"] = {
            "servers": [
                "1.1.1.1",
                "8.8.8.8",
                "8.8.4.4",
            ]
        }

        if include_stats_api:
            config["policy"] = {
                "levels": {
                    "0": {
                        "statsUserUplink": True,
                        "statsUserDownlink": True,
                    }
                },
                "system": {
                    "statsInboundUplink": True,
                    "statsInboundDownlink": True,
                    "statsOutboundUplink": True,
                    "statsOutboundDownlink": True,
                },
            }

        config["inbounds"] = [
            {
                "listen": "0.0.0.0",
                "port": http_port,
                "protocol": "http",
                "settings": {
                    "allowTransparent": True,
                    "timeout": 300,
                },
                "sniffing": {},
                "tag": "http_IN",
            },
            {
                "listen": "0.0.0.0",
                "port": socks_port,
                "protocol": "socks",
                "settings": {
                    "auth": "noauth",
                    "ip": "0.0.0.0",
                    "udp": True,
                },
                "sniffing": {},
                "tag": "socks_IN",
            },
        ]
        config["outbounds"] = [
            outbound,
            {
                "protocol": "freedom",
                "sendThrough": "0.0.0.0",
                "settings": {
                    "domainStrategy": "AsIs",
                    "redirect": ":0",
                },
                "streamSettings": {},
                "tag": "DIRECT",
            },
            {
                "protocol": "blackhole",
                "sendThrough": "0.0.0.0",
                "settings": {
                    "response": {
                        "type": "none",
                    }
                },
                "streamSettings": {},
                "tag": "BLACKHOLE",
            },
        ]
        config["routing"] = {
            "domainStrategy": "AsIs",
            "domainMatcher": "mph",
            "rules": [
                {
                    "ip": [
                        "geoip:private",
                    ],
                    "outboundTag": "DIRECT",
                    "type": "field",
                },
                {
                    "ip": [
                        "geoip:cn",
                    ],
                    "outboundTag": "DIRECT",
                    "type": "field",
                },
                {
                    "domain": [
                        "geosite:cn",
                    ],
                    "outboundTag": "DIRECT",
                    "type": "field",
                },
            ],
        }

        if include_stats_api:
            config["inbounds"].append(
                {
                    "tag": "api",
                    "port": api_port,
                    "listen": "0.0.0.0",
                    "protocol": "dokodemo-door",
                    "settings": {
                        "udp": False,
                        "address": "0.0.0.0",
                        "allowTransparent": False,
                    },
                }
            )
            config["routing"]["rules"].insert(
                0,
                {
                    "inboundTag": [
                        "api",
                    ],
                    "outboundTag": "api",
                    "type": "field",
                    "enabled": True,
                },
            )

        return config

    @staticmethod
    def to_xray_outbound(parsed_config: Dict[str, Any]) -> Dict[str, Any]:
        """将解析的配置转换为 Xray outbound 格式"""
        protocol = parsed_config.get('protocol', '').lower()

        if protocol == 'vmess':
            return XrayConfigHandler.vmess_to_xray_outbound(parsed_config)
        elif protocol == 'vless':
            return XrayConfigHandler.vless_to_xray_outbound(parsed_config)
        elif protocol == 'trojan':
            return XrayConfigHandler.trojan_to_xray_outbound(parsed_config)
        elif protocol == 'shadowsocks':
            return XrayConfigHandler.shadowsocks_to_xray_outbound(parsed_config)
        else:
            raise ValueError(f"不支持的协议: {protocol}")
