#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Xray 服务创建器
自动创建 xray 服务配置，更新 docker-compose.yml 和 prometheus.yml

端口检查只能看到 docker-compose.yml 中各服务发布到主机的端口 (ports:)。
下列服务占用 supp VM 的主机端口，但在 docker-compose.yml 的 ports: 中看不到，
因此登记在 RESERVED_HOST_PORTS 中 (端口变化时请同步更新):
  - frps (network_mode: host): bind_port / quic_bind_port 7000、dashboard_port 7500 (见 frp/frps.ini)
  - rustdesk hbbs/hbbr (network_mode: host): 21115-21119 (rustdesk 默认端口)
  - Harbor (独立的 docker compose，不在本文件中): 50000 (见 harbor/harbor.yml 的 http.port)
  - node-exporter (services/node-exporter/docker-compose.yaml，独立的 docker compose): 9100
frp 用户隧道的 remote_port 由用户自行选择，脚本无法得知；
必要时在 supp VM 上用 `ss -ltn | grep :<port>` 再确认一次。
"""

import sys
import json
import re
import argparse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml  # PyYAML (supp VM 上为 5.3.1)
except ImportError:  # 没有 PyYAML 时使用基于正则的简单解析
    yaml = None

# 添加脚本目录到路径 (resolve(): Python 3.8 下 __file__ 是命令行上输入的相对路径)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from XrayConfigHandler import DEFAULT_API_PORT, DEFAULT_HTTP_PORT, DEFAULT_SOCKS_PORT, XrayConfigHandler


# 容器内固定端口 (现有 compose 服务和 exporter 都依赖这些值)
CONTAINER_HTTP_PORT = DEFAULT_HTTP_PORT     # 8889
CONTAINER_SOCKS_PORT = DEFAULT_SOCKS_PORT   # 1089
API_PORT = DEFAULT_API_PORT                 # 10085
EXPORTER_PORT = 9550

# 在 docker-compose.yml 的 ports: 中看不到、但占用主机端口的服务 (见模块说明)
RESERVED_HOST_PORTS = {
    7000: 'frps bind_port/quic_bind_port (network_mode: host)',
    7500: 'frps dashboard_port (network_mode: host)',
    21115: 'rustdesk hbbs (network_mode: host)',
    21116: 'rustdesk hbbs (network_mode: host)',
    21117: 'rustdesk hbbr (network_mode: host)',
    21118: 'rustdesk hbbs (network_mode: host)',
    21119: 'rustdesk hbbr (network_mode: host)',
    9100: 'node-exporter (services/node-exporter/docker-compose.yaml，独立的 docker compose)',
    50000: 'Harbor (独立的 docker compose)',
}

# 服务名: 小写字母、数字、'-'、'_' (现有服务名中有 '_')
SERVICE_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9_-]*$')


def _port_range(text: Any) -> List[int]:
    """'8080' -> [8080]，'8000-8002' -> [8000, 8001, 8002]；无法识别 (如 ${VAR}) 时返回 []"""
    match = re.fullmatch(r'(\d+)(?:-(\d+))?', str(text).strip())
    if not match:
        return []
    start = int(match.group(1))
    end = int(match.group(2) or start)
    return list(range(start, end + 1))


def published_host_ports(entry: Any) -> List[int]:
    """
    返回一个 compose ports 条目发布到主机的端口 (只看主机侧)。
    支持: 80:80、"443:443"、'127.0.0.1:8080:80'、"[::1]:8080:80"、8080:80/udp、
          8000-8010:8000-8010、长格式 {target: 80, published: 8080}。
    只写容器端口 (如 "8081") 或 "127.0.0.1::80" 时由 Docker 随机分配主机端口，返回 []。
    """
    if isinstance(entry, dict):
        published = entry.get('published')
        return _port_range(published) if published not in (None, '') else []

    text = str(entry).strip().strip('"\'')
    text = text.split('/', 1)[0]  # 去掉 /tcp、/udp
    if text.startswith('['):
        # [IPv6]:host:container
        text = text.partition(']')[2].lstrip(':')
    parts = text.split(':')
    if len(parts) == 2:
        host = parts[0]
    elif len(parts) == 3:
        host = parts[1]
    else:
        return []
    return _port_range(host)


def _scan_compose_services(text: str) -> Dict[str, List[Any]]:
    """
    不依赖 PyYAML 的简单扫描: 返回 {服务名: [ports 条目, ...]}。
    只看顶层 services: 下的服务键 (缩进与第一个服务相同) 及其 ports: 列表，
    支持短格式 (可带引号) 和长格式 (published:)。
    """
    services = {}  # type: Dict[str, List[Any]]
    in_services = False
    service_indent = None
    current = None
    ports_indent = None

    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith('#'):
            continue
        indent = len(raw) - len(raw.lstrip(' '))

        if indent == 0:
            in_services = re.match(r'^services:\s*(#.*)?$', raw) is not None
            service_indent = None
            current = None
            ports_indent = None
            continue
        if not in_services:
            continue

        if service_indent is None:
            service_indent = indent
        if indent <= service_indent:
            current = None
            ports_indent = None
            key_match = re.match(r'^\s*([A-Za-z0-9._-]+)\s*:\s*(#.*)?$', raw)
            if indent == service_indent and key_match:
                current = key_match.group(1)
                services[current] = []
            continue
        if current is None:
            continue

        if ports_indent is not None and indent <= ports_indent:
            ports_indent = None
        if ports_indent is None:
            if re.match(r'^\s+ports:\s*(#.*)?$', raw):
                ports_indent = indent
            continue

        # ports: 列表中的条目
        item = re.sub(r'\s+#.*$', '', stripped)
        published = re.match(r'^(?:-\s*)?published\s*:\s*(.+)$', item)
        if published:
            services[current].append({'published': published.group(1).strip().strip('"\'')})
        elif item.startswith('-') and not re.match(r'^-\s*[A-Za-z_]+\s*:', item):
            services[current].append(item[1:].strip())

    return services


def compose_services(text: str) -> Dict[str, List[Any]]:
    """返回 docker-compose.yml 中的 {服务名: [ports 条目, ...]}"""
    if yaml is None:
        return _scan_compose_services(text)
    # BaseLoader 把所有标量都当作字符串，避免 YAML 1.1 把未加引号的 22:22 解析成 60 进制整数
    data = yaml.load(text, Loader=yaml.BaseLoader) or {}
    services = data.get('services') or {}
    result = {}
    for name, service in services.items():
        ports = service.get('ports') if isinstance(service, dict) else None
        result[name] = list(ports) if isinstance(ports, list) else []
    return result


def host_port_owners(services: Dict[str, List[Any]]) -> Dict[int, str]:
    """{主机端口: 占用它的服务}，包括 RESERVED_HOST_PORTS"""
    owners = dict(RESERVED_HOST_PORTS)
    for name, ports in services.items():
        for entry in ports:
            for port in published_host_ports(entry):
                owners.setdefault(port, name)
    return owners


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
            # 先 resolve()：Python 3.8 (supp VM) 下 __file__ 是相对路径 (如 create_xray_service.py)，
            # 否则在 scripts/ 或 xray/ 下运行时会找不到 docker-compose.yml
            self.base_dir = Path(__file__).resolve().parent.parent.parent
        else:
            self.base_dir = Path(base_dir)

        self.xray_dir = self.base_dir / "xray"
        self.docker_compose_file = self.base_dir / "docker-compose.yml"
        self.prometheus_file = self.base_dir / "prometheus" / "prometheus.yml"

    def check_port_available(self, port: int) -> Tuple[bool, Optional[str]]:
        """
        检查主机端口是否已被 docker-compose.yml 中的服务发布，或被 RESERVED_HOST_PORTS 中的服务占用
        (network_mode: host 的服务和 Harbor 在 docker-compose.yml 中看不到，见模块说明)

        Returns:
            (是否可用, 占用该端口的服务名)
        """
        services = {}
        if self.docker_compose_file.exists():
            services = compose_services(self._read_text(self.docker_compose_file))
        owner = host_port_owners(services).get(port)
        return owner is None, owner

    def generate_xray_config(
        self,
        outbound: Dict[str, Any],
        http_port: int,
        socks_port: int,
        service_name: str
    ) -> Dict[str, Any]:
        """
        生成完整的 Xray 配置文件 (使用 XrayConfigHandler.build_xray_config 这一份模板)

        Args:
            outbound: Xray outbound 配置
            http_port: HTTP 代理端口（主机端口，未使用）
            socks_port: SOCKS5 代理端口（主机端口，未使用）
            service_name: 服务名称（未使用）

        注意：容器内固定使用 1089 (SOCKS5) 和 8889 (HTTP)
        """
        return XrayConfigHandler.build_xray_config(
            outbound,
            http_port=CONTAINER_HTTP_PORT,
            socks_port=CONTAINER_SOCKS_PORT,
            loglevel="none",
            access_log="/var/log/xray/access.log",
            error_log="/var/log/xray/error.log",
            include_stats_api=True,
            api_port=API_PORT,
        )

    def render_docker_compose(
        self,
        compose_text: str,
        service_name: str,
        http_port: int,
        socks_port: int
    ) -> str:
        """
        返回添加了 xray 服务和 exporter 服务之后的 docker-compose.yml 内容 (不写文件)
        """
        lines = compose_text.splitlines(True)

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
            f'      - {socks_port}:{CONTAINER_SOCKS_PORT}\n',
            f'      - {http_port}:{CONTAINER_HTTP_PORT}\n',
            '    volumes: \n',
            f'      - ./xray/{service_name}/config:/etc/xray\n',
            f'      - ./xray/{service_name}/log:/var/log/xray\n',
            '    expose:\n',
            f'      - {API_PORT}\n',
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
            f'    command: \'v2ray-exporter --v2ray-endpoint "{xray_service_name}:{API_PORT}" --listen ":{EXPORTER_PORT}"\'\n',
            '    expose:\n',
            f'      - {EXPORTER_PORT}\n',
            '\n'
        ]

        # 找到最后一个 xray-exporter 服务的位置
        insert_pos = len(lines)
        found_exporter = False

        # 从后往前查找最后一个 xray-exporter 服务 (服务名中可能有 '_')
        for i in range(len(lines) - 1, -1, -1):
            line = lines[i].rstrip()
            # 匹配服务定义行: "  xray-xxx-exporter:"
            if re.match(r'^\s+xray-[a-z0-9_-]+-exporter:\s*$', line):
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
                if re.match(r'^\s+xray-[a-z0-9_-]+:\s*$', line) and '-exporter' not in line:
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
        return ''.join(new_lines)

    def render_prometheus_config(self, prometheus_text: str, service_name: str) -> Optional[str]:
        """
        返回在 v2ray job 的 targets 末尾添加 xray-exporter 目标之后的 prometheus.yml 内容 (不写文件)。
        目标已存在时返回 None。
        """
        exporter_service_name = f"xray-{service_name}-exporter"
        new_target = f'          - "{exporter_service_name}:{EXPORTER_PORT}"'

        # 查找 v2ray job 的 targets 部分
        pattern = r'(- job_name: "v2ray".*?static_configs:\s*\n\s+- targets:\s*\n)((?:\s+- "[^"]+"\s*\n)*)'
        match = re.search(pattern, prometheus_text, re.DOTALL)

        if not match:
            raise ValueError("无法找到 v2ray job 配置")
        # v2ray job 不是预期的格式时，非贪婪匹配会跨进下一个 job (如 det-master)
        if '- job_name:' in match.group(1)[len('- job_name:'):]:
            raise ValueError("v2ray job 的格式与预期不同 (static_configs 下应直接是 '- targets:' 列表)，请手动添加目标")

        # 在 targets 列表末尾添加
        targets_section = match.group(2)
        # 检查是否已存在
        if f'"{exporter_service_name}:{EXPORTER_PORT}"' in targets_section:
            print(f"⚠  Prometheus 配置中已存在 {exporter_service_name}")
            return None
        # 在最后一个 target 后添加
        new_targets = targets_section.rstrip() + f'\n{new_target}\n'
        return prometheus_text[:match.start()] + match.group(1) + new_targets + prometheus_text[match.end(2):]

    def validate_rendered_compose(self, old_text: str, new_text: str, service_name: str) -> None:
        """
        检查渲染结果: 新服务必须位于 services 下，且除新增的两个服务外文件内容不变
        """
        new_keys = (f"xray-{service_name}", f"xray-{service_name}-exporter")
        if yaml is None:
            services = _scan_compose_services(new_text)
            missing = [key for key in new_keys if key not in services]
            if missing:
                raise ValueError(f"渲染后的 docker-compose.yml 中 services 下缺少 {', '.join(missing)}")
            return

        old = yaml.safe_load(old_text)
        new = yaml.safe_load(new_text)
        new_services = dict((new or {}).get('services') or {})
        missing = [key for key in new_keys if key not in new_services]
        if missing:
            raise ValueError(f"渲染后的 docker-compose.yml 中 services 下缺少 {', '.join(missing)}")
        for key in new_keys:
            del new_services[key]
        rest = dict(new)
        rest['services'] = new_services
        if rest != old:
            raise ValueError("渲染后的 docker-compose.yml 除新增服务外还有其他变化，已停止")

    def validate_rendered_prometheus(self, old_text: str, new_text: str, service_name: str) -> None:
        """
        检查渲染结果: 新目标必须位于 v2ray job 的 targets 中，且除此之外文件内容不变
        """
        if yaml is None:
            # 没有 PyYAML 时只能依赖 render_prometheus_config 中的格式检查
            return
        target = f"xray-{service_name}-exporter:{EXPORTER_PORT}"
        old = yaml.safe_load(old_text)
        new = yaml.safe_load(new_text)
        for job in (new or {}).get('scrape_configs') or []:
            if job.get('job_name') != 'v2ray':
                continue
            for static_config in job.get('static_configs') or []:
                targets = static_config.get('targets') or []
                if target in targets:
                    targets.remove(target)
                    if new != old:
                        raise ValueError("渲染后的 prometheus.yml 除新增目标外还有其他变化，已停止")
                    return
        raise ValueError(f"渲染后的 prometheus.yml 中 v2ray job 下没有 {target}，已停止")

    def validate_service_name(self, service_name: str) -> None:
        """服务名只能包含小写字母、数字、'-'、'_'，并以字母或数字开头"""
        if not SERVICE_NAME_RE.fullmatch(service_name or ''):
            raise ValueError(
                f"服务名称无效: {service_name!r} (只能包含小写字母、数字、'-'、'_'，并以字母或数字开头)"
            )

    def validate_ports(self, http_port: int, socks_port: int) -> None:
        """端口必须在 1-65535 之间，且 HTTP 与 SOCKS5 端口不能相同"""
        for label, port in (("HTTP", http_port), ("SOCKS5", socks_port)):
            if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
                raise ValueError(f"{label} 端口无效: {port!r} (应为 1-65535)")
        if http_port == socks_port:
            raise ValueError(f"HTTP 端口和 SOCKS5 端口不能相同: {http_port}")

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
        old_text = self._read_text(self.docker_compose_file)
        new_text = self.render_docker_compose(old_text, service_name, http_port, socks_port)
        self.validate_rendered_compose(old_text, new_text, service_name)
        self._write_text(self.docker_compose_file, new_text)
        print("✓ 已更新 docker-compose.yml")

    def update_prometheus_config(self, service_name: str) -> None:
        """
        更新 prometheus.yml，添加 xray-exporter 目标
        """
        if not self.prometheus_file.exists():
            raise FileNotFoundError(f"找不到 prometheus.yml: {self.prometheus_file}")
        old_text = self._read_text(self.prometheus_file)
        new_text = self.render_prometheus_config(old_text, service_name)
        if new_text is None:
            return
        self.validate_rendered_prometheus(old_text, new_text, service_name)
        self._write_text(self.prometheus_file, new_text)
        print("✓ 已更新 prometheus.yml")

    @staticmethod
    def _read_text(path: Path) -> str:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()

    @staticmethod
    def _write_text(path: Path, text: str) -> None:
        # 原地写入 (不改变 inode)：prometheus.yml 可能被单独 bind mount 到其他容器
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)

    def create_service(
        self,
        share_url: str,
        service_name: str,
        http_port: int,
        socks_port: int
    ) -> None:
        """
        创建完整的 xray 服务

        先校验参数、生成并检查全部三个文件的新内容，全部通过后才写入；
        写入过程中出错时恢复已写入的文件。

        Args:
            share_url: Xray 分享 URL
            service_name: 简短的服务名称（如: jp-osaka-xuqi）
            http_port: HTTP 代理端口
            socks_port: SOCKS5 代理端口
        """
        print(f"正在创建 xray 服务: {service_name}")
        print(f"HTTP 端口: {http_port}, SOCKS5 端口: {socks_port}")

        # 检查参数
        self.validate_service_name(service_name)
        self.validate_ports(http_port, socks_port)

        if not self.docker_compose_file.exists():
            raise FileNotFoundError(f"找不到 docker-compose.yml: {self.docker_compose_file}")
        if not self.prometheus_file.exists():
            raise FileNotFoundError(f"找不到 prometheus.yml: {self.prometheus_file}")
        compose_text = self._read_text(self.docker_compose_file)
        prometheus_text = self._read_text(self.prometheus_file)
        if yaml is None:
            print("⚠  未安装 PyYAML，使用简单的正则解析 docker-compose.yml")
        services = compose_services(compose_text)

        # 检查服务是否已存在
        for key in (f"xray-{service_name}", f"xray-{service_name}-exporter"):
            if key in services:
                raise ValueError(f"服务 {key} 已存在于 docker-compose.yml")
        service_dir = self.xray_dir / service_name
        if service_dir.exists():
            raise ValueError(f"目录已存在: {service_dir} (为避免覆盖已有配置，请换一个服务名称)")

        # 检查端口占用
        owners = host_port_owners(services)
        if http_port in owners:
            raise ValueError(f"HTTP 端口 {http_port} 已被服务 {owners[http_port]} 占用")
        if socks_port in owners:
            raise ValueError(f"SOCKS5 端口 {socks_port} 已被服务 {owners[socks_port]} 占用")

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
        config_text = json.dumps(xray_config, indent=4, ensure_ascii=False)

        # 生成并检查 docker-compose.yml 和 prometheus.yml 的新内容
        print("正在生成 docker-compose.yml 和 prometheus.yml 的新内容...")
        new_compose_text = self.render_docker_compose(compose_text, service_name, http_port, socks_port)
        self.validate_rendered_compose(compose_text, new_compose_text, service_name)
        new_prometheus_text = self.render_prometheus_config(prometheus_text, service_name)
        if new_prometheus_text is not None:
            self.validate_rendered_prometheus(prometheus_text, new_prometheus_text, service_name)

        # 写入文件；任何一步失败都恢复已写入的内容
        config_dir = service_dir / "config"
        log_dir = service_dir / "log"
        config_file = config_dir / "config.json"
        created_dirs = []  # type: List[Path]
        written_files = []  # type: List[Tuple[Path, str]]
        config_written = False
        try:
            for directory in (self.xray_dir, service_dir, config_dir, log_dir):
                if not directory.exists():
                    directory.mkdir()
                    created_dirs.append(directory)

            # 先登记再写入: 写到一半失败 (文件已被截断) 时也能恢复
            # 保存配置文件
            config_written = True
            self._write_text(config_file, config_text)
            print(f"✓ 已创建配置文件: {config_file}")

            # 更新 docker-compose.yml
            written_files.append((self.docker_compose_file, compose_text))
            self._write_text(self.docker_compose_file, new_compose_text)
            print("✓ 已更新 docker-compose.yml")

            # 更新 prometheus.yml
            if new_prometheus_text is not None:
                written_files.append((self.prometheus_file, prometheus_text))
                self._write_text(self.prometheus_file, new_prometheus_text)
                print("✓ 已更新 prometheus.yml")
        except BaseException:
            print("❌ 写入失败，正在恢复已修改的文件...", file=sys.stderr)
            for path, original in reversed(written_files):
                self._write_text(path, original)
            if config_written and config_file.exists():
                config_file.unlink()
            for directory in reversed(created_dirs):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            raise

        print(f"\n✓ 服务 {service_name} 创建成功！")
        print("\n下一步 (在 services/ 目录下执行):")
        print(f"  1. 检查配置文件: {config_file}")
        print(f"  2. 启动服务: docker compose up -d xray-{service_name} xray-{service_name}-exporter")
        print("  3. 重新加载 Prometheus 配置: docker compose kill -s SIGHUP prometheus")
        print(f"  4. 验证代理: curl -x http://localhost:{http_port} -I https://www.google.com")
        print(f"            curl --socks5-hostname localhost:{socks_port} -I https://www.google.com")


def main():
    parser = argparse.ArgumentParser(
        description='创建 Xray 服务',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s vmess://... jp-tokyo 56889 56089
  %(prog)s vless://... us-east 41889 41089
        """
    )

    parser.add_argument(
        'share_url',
        help='Xray 分享 URL (vmess://, vless://, trojan://, ss://)'
    )

    parser.add_argument(
        'service_name',
        help='简短的服务名称 (如: jp-osaka-xuqi；小写字母、数字、-、_)'
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
