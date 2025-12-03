#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Xray 服务创建器
自动创建 xray 服务配置，更新 docker-compose.yml 和 prometheus.yml
"""

import os
import sys
import json
import re
import argparse
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional

# 添加脚本目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from XrayConfigHandler import XrayConfigHandler


class XrayServiceCreator:
    """Xray 服务创建器"""
    
    def __init__(self, base_dir: str = None):
        """
        初始化服务创建器
        
        Args:
            base_dir: 服务根目录，默认为脚本所在目录的父目录的父目录
        """
        if base_dir is None:
            # 默认: services/xray/scripts -> services/
            self.base_dir = Path(__file__).parent.parent.parent
        else:
            self.base_dir = Path(base_dir)
        
        self.xray_dir = self.base_dir / "xray"
        self.docker_compose_file = self.base_dir / "docker-compose.yml"
        self.prometheus_file = self.base_dir / "prometheus" / "prometheus.yml"
    
    def check_port_available(self, port: int) -> Tuple[bool, Optional[str]]:
        """
        检查端口是否在 docker-compose.yml 中已被占用
        
        Returns:
            (是否可用, 占用该端口的服务名)
        """
        if not self.docker_compose_file.exists():
            return True, None
        
        with open(self.docker_compose_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        current_service = None
        in_ports_section = False
        
        for i, line in enumerate(lines):
            # 检测服务定义
            service_match = re.match(r'^\s+([a-zA-Z0-9_-]+):\s*$', line)
            if service_match:
                current_service = service_match.group(1)
                in_ports_section = False
                continue
            
            # 检测 ports: 部分
            if re.match(r'^\s+ports:\s*$', line):
                in_ports_section = True
                continue
            
            # 在 ports 部分检查端口映射
            if in_ports_section:
                # 匹配端口映射格式: "  - host_port:container_port" 或 "  - port:port"
                port_match = re.match(r'^\s+-\s+(\d+):(\d+)', line)
                if port_match:
                    host_port = int(port_match.group(1))
                    container_port = int(port_match.group(2))
                    if host_port == port or container_port == port:
                        return False, current_service
                # 如果遇到其他顶级键，退出 ports 部分
                elif re.match(r'^\s+[a-zA-Z]', line) and not line.strip().startswith('-'):
                    in_ports_section = False
        
        return True, None
    
    def generate_xray_config(
        self,
        outbound: Dict[str, Any],
        http_port: int,
        socks_port: int,
        service_name: str
    ) -> Dict[str, Any]:
        """
        生成完整的 Xray 配置文件
        
        Args:
            outbound: Xray outbound 配置
            http_port: HTTP 代理端口（主机端口，用于显示）
            socks_port: SOCKS5 代理端口（主机端口，用于显示）
            service_name: 服务名称（用于 tag）
        
        注意：容器内固定使用 1089 (SOCKS5) 和 8889 (HTTP)
        """
        # 容器内固定端口
        CONTAINER_SOCKS_PORT = 1089
        CONTAINER_HTTP_PORT = 8889
        
        config = {
            "log": {
                "loglevel": "none",
                "access": "/var/log/xray/access.log",
                "error": "/var/log/xray/error.log"
            },
            "stats": {},
            "api": {
                "tag": "api",
                "services": [
                    "StatsService"
                ]
            },
            "dns": {
                "servers": [
                    "1.1.1.1",
                    "8.8.8.8",
                    "8.8.4.4"
                ]
            },
            "policy": {
                "levels": {
                    "0": {
                        "statsUserUplink": True,
                        "statsUserDownlink": True
                    }
                },
                "system": {
                    "statsInboundUplink": True,
                    "statsInboundDownlink": True,
                    "statsOutboundUplink": True,
                    "statsOutboundDownlink": True
                }
            },
            "inbounds": [
                {
                    "listen": "0.0.0.0",
                    "port": CONTAINER_HTTP_PORT,
                    "protocol": "http",
                    "settings": {
                        "allowTransparent": True,
                        "timeout": 300
                    },
                    "sniffing": {},
                    "tag": "http_IN"
                },
                {
                    "listen": "0.0.0.0",
                    "port": CONTAINER_SOCKS_PORT,
                    "protocol": "socks",
                    "settings": {
                        "auth": "noauth",
                        "ip": "0.0.0.0",
                        "udp": True
                    },
                    "sniffing": {},
                    "tag": "socks_IN"
                },
                {
                    "tag": "api",
                    "port": 10085,
                    "listen": "0.0.0.0",
                    "protocol": "dokodemo-door",
                    "settings": {
                        "udp": False,
                        "address": "0.0.0.0",
                        "allowTransparent": False
                    }
                }
            ],
            "outbounds": [
                outbound,
                {
                    "protocol": "freedom",
                    "sendThrough": "0.0.0.0",
                    "settings": {
                        "domainStrategy": "AsIs",
                        "redirect": ":0"
                    },
                    "streamSettings": {},
                    "tag": "DIRECT"
                },
                {
                    "protocol": "blackhole",
                    "sendThrough": "0.0.0.0",
                    "settings": {
                        "response": {
                            "type": "none"
                        }
                    },
                    "streamSettings": {},
                    "tag": "BLACKHOLE"
                }
            ],
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
                        "enabled": True
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
        
        return config
    
    def update_docker_compose(
        self,
        service_name: str,
        http_port: int,
        socks_port: int
    ) -> None:
        """
        更新 docker-compose.yml，添加 xray 服务和 exporter 服务
        """
        if not self.docker_compose_file.exists():
            raise FileNotFoundError(f"找不到 docker-compose.yml: {self.docker_compose_file}")
        
        with open(self.docker_compose_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        # 生成服务配置
        xray_service_name = f"xray-{service_name}"
        exporter_service_name = f"xray-{service_name}-exporter"
        
        # 容器内固定端口：1089 (SOCKS5), 8889 (HTTP)
        xray_service_lines = [
            f'  {xray_service_name}:\n',
            '    image: teddysun/xray:latest\n',
            '    restart: unless-stopped\n',
            '    environment:\n',
            '      TZ: Asia/Shanghai\n',
            '    networks:\n',
            '      - grafana_monitor\n',
            '    ports:\n',
            f'      - {socks_port}:1089\n',
            f'      - {http_port}:8889\n',
            '    volumes: \n',
            f'      - ./xray/{service_name}/config:/etc/xray\n',
            f'      - ./xray/{service_name}/log:/var/log/xray\n',
            '    expose:\n',
            '      - 10085\n',
            '\n'
        ]
        
        exporter_service_lines = [
            f'  {exporter_service_name}:\n',
            '    image: wi1dcard/v2ray-exporter:master\n',
            '    environment:\n',
            '      TZ: Asia/Shanghai\n',
            '    networks:\n',
            '      - grafana_monitor\n',
            '    restart: unless-stopped\n',
            f'    command: \'v2ray-exporter --v2ray-endpoint "{xray_service_name}:10085" --listen ":9550"\'\n',
            '    expose:\n',
            '      - 9550\n',
            '\n'
        ]
        
        # 找到最后一个 xray-exporter 服务的位置
        insert_pos = len(lines)
        found_exporter = False
        
        # 从后往前查找最后一个 xray-exporter 服务
        for i in range(len(lines) - 1, -1, -1):
            line = lines[i].rstrip()
            # 匹配服务定义行: "  xray-xxx-exporter:"
            if re.match(r'^\s+xray-[a-z0-9-]+-exporter:\s*$', line):
                found_exporter = True
                # 找到这个服务的结束位置（下一个服务定义）
                j = i + 1
                while j < len(lines):
                    next_line = lines[j].rstrip()
                    # 如果遇到下一个服务定义（以两个空格开头，然后是字母或数字），停止
                    if re.match(r'^\s{2}[a-zA-Z0-9]', next_line):
                        insert_pos = j
                        break
                    j += 1
                else:
                    # 如果到文件末尾都没找到下一个服务，在末尾插入
                    insert_pos = len(lines)
                break
        
        # 如果没找到 exporter，尝试在最后一个 xray 服务后插入
        if not found_exporter:
            for i in range(len(lines) - 1, -1, -1):
                line = lines[i].rstrip()
                # 匹配 xray 服务（但不包括 exporter）
                if re.match(r'^\s+xray-[a-z0-9-]+:\s*$', line) and '-exporter' not in line:
                    # 找到这个服务的结束位置
                    j = i + 1
                    while j < len(lines):
                        next_line = lines[j].rstrip()
                        if re.match(r'^\s{2}[a-zA-Z0-9]', next_line):
                            insert_pos = j
                            break
                        j += 1
                    else:
                        insert_pos = len(lines)
                    break
        
        # 插入新服务
        new_lines = lines[:insert_pos] + xray_service_lines + exporter_service_lines + lines[insert_pos:]
        
        with open(self.docker_compose_file, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
        
        print(f"✓ 已更新 docker-compose.yml")
    
    def update_prometheus_config(self, service_name: str) -> None:
        """
        更新 prometheus.yml，添加 xray-exporter 目标
        """
        if not self.prometheus_file.exists():
            raise FileNotFoundError(f"找不到 prometheus.yml: {self.prometheus_file}")
        
        with open(self.prometheus_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        exporter_service_name = f"xray-{service_name}-exporter"
        new_target = f'          - "{exporter_service_name}:9550"'
        
        # 查找 v2ray job 的 targets 部分
        pattern = r'(- job_name: "v2ray".*?static_configs:\s*\n\s+- targets:\s*\n)((?:\s+- "[^"]+"\s*\n)*)'
        match = re.search(pattern, content, re.DOTALL)
        
        if match:
            # 在 targets 列表末尾添加
            targets_section = match.group(2)
            # 检查是否已存在
            if exporter_service_name not in targets_section:
                # 在最后一个 target 后添加
                new_targets = targets_section.rstrip() + f'\n{new_target}\n'
                new_content = content[:match.start()] + match.group(1) + new_targets + content[match.end(2):]
            else:
                print(f"⚠  Prometheus 配置中已存在 {exporter_service_name}")
                return
        else:
            raise ValueError("无法找到 v2ray job 配置")
        
        with open(self.prometheus_file, 'w', encoding='utf-8') as f:
            f.write(new_content)
        
        print(f"✓ 已更新 prometheus.yml")
    
    def create_service(
        self,
        share_url: str,
        service_name: str,
        http_port: int,
        socks_port: int
    ) -> None:
        """
        创建完整的 xray 服务
        
        Args:
            share_url: Xray 分享 URL
            service_name: 简短的服务名称（如: jp-osaka-xuqi）
            http_port: HTTP 代理端口
            socks_port: SOCKS5 代理端口
        """
        print(f"正在创建 xray 服务: {service_name}")
        print(f"HTTP 端口: {http_port}, SOCKS5 端口: {socks_port}")
        
        # 检查端口占用
        http_available, http_service = self.check_port_available(http_port)
        if not http_available:
            raise ValueError(f"HTTP 端口 {http_port} 已被服务 {http_service} 占用")
        
        socks_available, socks_service = self.check_port_available(socks_port)
        if not socks_available:
            raise ValueError(f"SOCKS5 端口 {socks_port} 已被服务 {socks_service} 占用")
        
        print("✓ 端口检查通过")
        
        # 解析分享 URL
        print("正在解析分享 URL...")
        parsed_config = XrayConfigHandler.parse_share_url(share_url)
        print(f"✓ 协议: {parsed_config.get('protocol', 'unknown')}")
        
        # 转换为 Xray outbound 配置
        outbound = XrayConfigHandler.to_xray_outbound(parsed_config)
        
        # 生成完整配置
        print("正在生成 Xray 配置...")
        xray_config = self.generate_xray_config(outbound, http_port, socks_port, service_name)
        
        # 创建目录
        service_dir = self.xray_dir / service_name
        config_dir = service_dir / "config"
        log_dir = service_dir / "log"
        
        config_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        
        # 保存配置文件
        config_file = config_dir / "config.json"
        with open(config_file, 'w', encoding='utf-8') as f:
            json.dump(xray_config, f, indent=4, ensure_ascii=False)
        
        print(f"✓ 已创建配置文件: {config_file}")
        
        # 更新 docker-compose.yml
        print("正在更新 docker-compose.yml...")
        self.update_docker_compose(service_name, http_port, socks_port)
        
        # 更新 prometheus.yml
        print("正在更新 prometheus.yml...")
        self.update_prometheus_config(service_name)
        
        print(f"\n✓ 服务 {service_name} 创建成功！")
        print(f"\n下一步:")
        print(f"  1. 检查配置文件: {config_file}")
        print(f"  2. 运行: docker-compose up -d xray-{service_name} xray-{service_name}-exporter")
        print(f"  3. 验证服务: curl http://localhost:{http_port}")


def main():
    parser = argparse.ArgumentParser(
        description='创建 Xray 服务',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s vmess://... jp-tokyo 8080 1080
  %(prog)s vless://... us-east 8081 1081
        """
    )
    
    parser.add_argument(
        'share_url',
        help='Xray 分享 URL (vmess://, vless://, trojan://, ss://)'
    )
    
    parser.add_argument(
        'service_name',
        help='简短的服务名称 (如: jp-osaka-xuqi)'
    )
    
    parser.add_argument(
        'http_port',
        type=int,
        help='HTTP 代理端口'
    )
    
    parser.add_argument(
        'socks_port',
        type=int,
        help='SOCKS5 代理端口'
    )
    
    parser.add_argument(
        '--base-dir',
        type=str,
        default=None,
        help='服务根目录 (默认: 自动检测)'
    )
    
    args = parser.parse_args()
    
    try:
        creator = XrayServiceCreator(base_dir=args.base_dir)
        creator.create_service(
            args.share_url,
            args.service_name,
            args.http_port,
            args.socks_port
        )
    except Exception as e:
        print(f"❌ 错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()

