#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XrayConfigHandler - 解析 Xray 分享 URL 并生成配置
参考: https://github.com/2dust/v2rayN/blob/master/v2rayN/ServiceLib/Handler/ConfigHandler.cs
"""

import base64
import json
import urllib.parse
from typing import Dict, Any, Optional


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
            parsed = urllib.parse.urlparse(url)
            config = {
                'id': parsed.username,
                'address': parsed.hostname,
                'port': parsed.port,
                'protocol': 'vless',
                'remark': urllib.parse.unquote(parsed.fragment) if parsed.fragment else ''
            }
            
            # 解析查询参数
            params = urllib.parse.parse_qs(parsed.query)
            for key, value in params.items():
                if len(value) == 1:
                    config[key] = value[0]
                else:
                    config[key] = value
            
            return config
        except Exception as e:
            raise ValueError(f"解析 vless URL 失败: {e}")
    
    @staticmethod
    def parse_trojan_url(url: str) -> Dict[str, Any]:
        """
        解析 trojan:// URL
        格式: trojan://password@host:port?params#remark
        """
        try:
            parsed = urllib.parse.urlparse(url)
            config = {
                'password': parsed.username,
                'address': parsed.hostname,
                'port': parsed.port,
                'protocol': 'trojan',
                'remark': urllib.parse.unquote(parsed.fragment) if parsed.fragment else ''
            }
            
            # 解析查询参数
            params = urllib.parse.parse_qs(parsed.query)
            for key, value in params.items():
                if len(value) == 1:
                    config[key] = value[0]
                else:
                    config[key] = value
            
            return config
        except Exception as e:
            raise ValueError(f"解析 trojan URL 失败: {e}")
    
    @staticmethod
    def parse_shadowsocks_url(url: str) -> Dict[str, Any]:
        """
        解析 ss:// URL
        格式: ss://base64(method:password)@host:port#remark
        或: ss://base64(method:password@host:port)#remark
        """
        try:
            parsed = urllib.parse.urlparse(url)
            
            # 处理两种格式
            if '@' in parsed.netloc:
                # 格式: ss://base64(method:password)@host:port
                auth_part, server_part = parsed.netloc.split('@', 1)
                decoded_auth = base64.urlsafe_b64decode(
                    auth_part + '=' * (-len(auth_part) % 4)
                ).decode('utf-8')
                method, password = decoded_auth.split(':', 1)
                host, port = server_part.rsplit(':', 1)
            else:
                # 格式: ss://base64(method:password@host:port)
                encoded = parsed.netloc
                decoded = base64.urlsafe_b64decode(
                    encoded + '=' * (-len(encoded) % 4)
                ).decode('utf-8')
                auth_server = decoded.split('@', 1)
                if len(auth_server) == 2:
                    method, password = auth_server[0].split(':', 1)
                    host, port = auth_server[1].rsplit(':', 1)
                else:
                    raise ValueError("无法解析 shadowsocks URL 格式")
            
            config = {
                'method': method,
                'password': password,
                'address': host,
                'port': int(port),
                'protocol': 'shadowsocks',
                'remark': urllib.parse.unquote(parsed.fragment) if parsed.fragment else ''
            }
            
            # 解析查询参数
            params = urllib.parse.parse_qs(parsed.query)
            for key, value in params.items():
                if len(value) == 1:
                    config[key] = value[0]
                else:
                    config[key] = value
            
            return config
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
        port = int(vmess_config.get('port') or 0)
        user_id = vmess_config.get('id') or vmess_config.get('uuid') or ''
        alter_id = int(vmess_config.get('aid') or vmess_config.get('alterId') or 0)
        security = vmess_config.get('scy') or vmess_config.get('security') or 'auto'
        level = int(vmess_config.get('v') or vmess_config.get('version') or 0)
        remark = vmess_config.get('ps') or vmess_config.get('remark') or 'vmess'
        
        outbound = {
            "protocol": "vmess",
            "sendThrough": "0.0.0.0",
            "settings": {
                "vnext": [
                    {
                        "address": address,
                        "port": port,
                        "users": [
                            {
                                "id": user_id,
                                "alterId": alter_id,
                                "security": security,
                                "level": level
                            }
                        ]
                    }
                ]
            },
            "streamSettings": {},
            "tag": remark
        }
        
        # 处理流设置
        network = vmess_config.get('net', vmess_config.get('network', 'tcp'))
        outbound["streamSettings"]["network"] = network
        
        # TLS/XTLS
        tls = vmess_config.get('tls', vmess_config.get('security', ''))
        if tls in ['tls', 'xtls', 'reality']:
            outbound["streamSettings"]["security"] = tls
            tls_settings = {}
            if vmess_config.get('sni') or vmess_config.get('host'):
                tls_settings["serverName"] = vmess_config.get('sni') or vmess_config.get('host')
            if vmess_config.get('alpn'):
                tls_settings["alpn"] = vmess_config.get('alpn').split(',') if isinstance(vmess_config.get('alpn'), str) else vmess_config.get('alpn')
            if tls_settings:
                outbound["streamSettings"][f"{tls}Settings"] = tls_settings
        
        # 网络类型特定设置
        if network == 'ws':
            ws_settings = {}
            if vmess_config.get('path'):
                ws_settings["path"] = vmess_config.get('path')
            if vmess_config.get('host'):
                ws_settings["headers"] = {"Host": vmess_config.get('host')}
            if ws_settings:
                outbound["streamSettings"]["wsSettings"] = ws_settings
        elif network == 'http':
            http_settings = {}
            if vmess_config.get('path'):
                http_settings["path"] = vmess_config.get('path')
            if vmess_config.get('host'):
                http_settings["host"] = [vmess_config.get('host')]
            if http_settings:
                outbound["streamSettings"]["httpSettings"] = http_settings
        elif network == 'grpc':
            grpc_settings = {}
            if vmess_config.get('path'):
                grpc_settings["serviceName"] = vmess_config.get('path')
            if grpc_settings:
                outbound["streamSettings"]["grpcSettings"] = grpc_settings
        elif network == 'quic':
            quic_settings = {}
            if vmess_config.get('type'):
                quic_settings["security"] = vmess_config.get('type')
            if vmess_config.get('key'):
                quic_settings["key"] = vmess_config.get('key')
            if vmess_config.get('path'):
                quic_settings["path"] = vmess_config.get('path')
            if quic_settings:
                outbound["streamSettings"]["quicSettings"] = quic_settings
        
        return outbound
    
    @staticmethod
    def vless_to_xray_outbound(vless_config: Dict[str, Any]) -> Dict[str, Any]:
        """将 vless 配置转换为 Xray outbound 格式"""
        outbound = {
            "protocol": "vless",
            "sendThrough": "0.0.0.0",
            "settings": {
                "vnext": [
                    {
                        "address": vless_config.get('address', ''),
                        "port": int(vless_config.get('port', 0)),
                        "users": [
                            {
                                "id": vless_config.get('id', ''),
                                "encryption": vless_config.get('encryption', 'none'),
                                "flow": vless_config.get('flow', '')
                            }
                        ]
                    }
                ]
            },
            "streamSettings": {},
            "tag": vless_config.get('remark', 'vless')
        }
        
        # 处理流设置
        network = vless_config.get('type', vless_config.get('network', 'tcp'))
        outbound["streamSettings"]["network"] = network
        
        # TLS/XTLS/Reality
        security = vless_config.get('security', vless_config.get('tls', ''))
        if security in ['tls', 'xtls', 'reality']:
            outbound["streamSettings"]["security"] = security
            security_settings = {}
            if vless_config.get('sni') or vless_config.get('host'):
                security_settings["serverName"] = vless_config.get('sni') or vless_config.get('host')
            if vless_config.get('alpn'):
                alpn_list = vless_config.get('alpn').split(',') if isinstance(vless_config.get('alpn'), str) else vless_config.get('alpn')
                security_settings["alpn"] = alpn_list
            if vless_config.get('fp'):
                security_settings["fingerprint"] = vless_config.get('fp')
            if security == 'reality':
                if vless_config.get('pbk'):
                    security_settings["publicKey"] = vless_config.get('pbk')
                if vless_config.get('sid'):
                    security_settings["shortId"] = vless_config.get('sid')
                if vless_config.get('spx'):
                    security_settings["spiderX"] = vless_config.get('spx')
            if security_settings:
                outbound["streamSettings"][f"{security}Settings"] = security_settings
        
        # 网络类型特定设置
        if network == 'ws':
            ws_settings = {}
            if vless_config.get('path'):
                ws_settings["path"] = vless_config.get('path')
            if vless_config.get('host'):
                ws_settings["headers"] = {"Host": vless_config.get('host')}
            if ws_settings:
                outbound["streamSettings"]["wsSettings"] = ws_settings
        elif network == 'http':
            http_settings = {}
            if vless_config.get('path'):
                http_settings["path"] = vless_config.get('path')
            if vless_config.get('host'):
                http_settings["host"] = [vless_config.get('host')]
            if http_settings:
                outbound["streamSettings"]["httpSettings"] = http_settings
        elif network == 'grpc':
            grpc_settings = {}
            if vless_config.get('serviceName') or vless_config.get('path'):
                grpc_settings["serviceName"] = vless_config.get('serviceName') or vless_config.get('path')
            if grpc_settings:
                outbound["streamSettings"]["grpcSettings"] = grpc_settings
        
        return outbound
    
    @staticmethod
    def trojan_to_xray_outbound(trojan_config: Dict[str, Any]) -> Dict[str, Any]:
        """将 trojan 配置转换为 Xray outbound 格式"""
        outbound = {
            "protocol": "trojan",
            "sendThrough": "0.0.0.0",
            "settings": {
                "servers": [
                    {
                        "address": trojan_config.get('address', ''),
                        "port": int(trojan_config.get('port', 0)),
                        "password": trojan_config.get('password', ''),
                        "email": trojan_config.get('email', '')
                    }
                ]
            },
            "streamSettings": {},
            "tag": trojan_config.get('remark', 'trojan')
        }
        
        # 处理流设置
        network = trojan_config.get('type', trojan_config.get('network', 'tcp'))
        outbound["streamSettings"]["network"] = network
        
        # TLS
        security = trojan_config.get('security', trojan_config.get('tls', 'tls'))
        if security == 'tls':
            outbound["streamSettings"]["security"] = security
            tls_settings = {}
            if trojan_config.get('sni') or trojan_config.get('host'):
                tls_settings["serverName"] = trojan_config.get('sni') or trojan_config.get('host')
            if trojan_config.get('alpn'):
                alpn_list = trojan_config.get('alpn').split(',') if isinstance(trojan_config.get('alpn'), str) else trojan_config.get('alpn')
                tls_settings["alpn"] = alpn_list
            if tls_settings:
                outbound["streamSettings"]["tlsSettings"] = tls_settings
        
        # 网络类型特定设置
        if network == 'ws':
            ws_settings = {}
            if trojan_config.get('path'):
                ws_settings["path"] = trojan_config.get('path')
            if trojan_config.get('host'):
                ws_settings["headers"] = {"Host": trojan_config.get('host')}
            if ws_settings:
                outbound["streamSettings"]["wsSettings"] = ws_settings
        elif network == 'grpc':
            grpc_settings = {}
            if trojan_config.get('serviceName') or trojan_config.get('path'):
                grpc_settings["serviceName"] = trojan_config.get('serviceName') or trojan_config.get('path')
            if grpc_settings:
                outbound["streamSettings"]["grpcSettings"] = grpc_settings
        
        return outbound
    
    @staticmethod
    def shadowsocks_to_xray_outbound(ss_config: Dict[str, Any]) -> Dict[str, Any]:
        """将 shadowsocks 配置转换为 Xray outbound 格式"""
        outbound = {
            "protocol": "shadowsocks",
            "sendThrough": "0.0.0.0",
            "settings": {
                "servers": [
                    {
                        "address": ss_config.get('address', ''),
                        "port": int(ss_config.get('port', 0)),
                        "method": ss_config.get('method', ''),
                        "password": ss_config.get('password', ''),
                        "email": ss_config.get('email', '')
                    }
                ]
            },
            "streamSettings": {},
            "tag": ss_config.get('remark', 'shadowsocks')
        }
        
        return outbound
    
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

