# -*- coding: utf-8 -*-
"""
create_xray_service.py: 端口检查、服务名/端口校验、渲染与校验、失败回滚。

每个测试类都分别用 PyYAML (已安装时) 和不依赖 PyYAML 的正则解析各跑一遍。
所有写入都发生在临时目录中的副本上。
fixtures/prometheus_sample.yml 是 services/prometheus/prometheus.yml (PR #3 之后) 的副本，
只有 v2ray job 的 targets 换成了 compose_sample.yml 中的两个 xray 服务。
"""

import contextlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

try:
    import yaml as pyyaml  # 只用于检查结果；不受下面对 cxs.yaml 的屏蔽影响
except ImportError:
    pyyaml = None

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import create_xray_service as cxs  # noqa: E402
from create_xray_service import XrayServiceCreator, compose_services, host_port_owners, published_host_ports  # noqa: E402
from XrayConfigHandler import XrayConfigHandler  # noqa: E402
from corpus import CREATE_SERVICE_TEMPLATE, FIXTURES_DIR, SCRIPTS_DIR, case_url  # noqa: E402

REPO_SERVICES_DIR = SCRIPTS_DIR.parent.parent  # services/
VLESS_URL = case_url('vless_reality_vision')
EXPORTER_RE = re.compile(r'^\s+xray-[a-z0-9_-]+-exporter:\s*$')
V2RAY_TARGET_RE = re.compile(r'^\s+- "xray-[a-z0-9_-]+-exporter:9550"$')
JOB_RE = re.compile(r'^\s*- job_name:')
# PR #3: det-master 的 token 文件 (watchdog 写入，Prometheus 以只读方式挂载)
METRICS_TOKEN_AUTH = '    authorization:\n      type: Bearer\n      credentials_file: /run/determined-metrics/token\n'


def read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def golden_block(name):
    """重构前的模板为 golden-test (56889/56089) 生成的两个服务块，换成新的服务名"""
    return read(FIXTURES_DIR / "compose_block_golden-test.txt").replace("golden-test", name)


class PublishedHostPortsTest(unittest.TestCase):

    def test_port_entry_forms(self):
        cases = [
            ('80:80', [80]),
            ('"443:443"', [443]),
            ("'8080:8080'", [8080]),
            ('8081:8080', [8081]),
            ('59889:8889', [59889]),
            ('127.0.0.1:6100:80', [6100]),
            ('[::1]:6400:80', [6400]),
            ('6200:53/udp', [6200]),
            ('6300-6302:7300-7302', [6300, 6301, 6302]),
            ('6500', []),                 # 只有容器端口: 主机端口随机
            (8081, []),
            ('127.0.0.1::6550', []),
            ('${PORT}:80', []),
            ({'target': '80', 'published': '6600'}, [6600]),
            ({'target': 80, 'published': 6600}, [6600]),
            ({'target': '80'}, []),
        ]
        for entry, expected in cases:
            with self.subTest(entry=entry):
                self.assertEqual(published_host_ports(entry), expected)


class _TempServicesMixin:
    """在临时目录中准备 services/ 副本；use_yaml=False 时屏蔽 PyYAML"""

    use_yaml = True
    compose_source = FIXTURES_DIR / "compose_sample.yml"
    prometheus_source = FIXTURES_DIR / "prometheus_sample.yml"

    def setUp(self):
        if self.use_yaml and cxs.yaml is None:
            self.skipTest("PyYAML 未安装")
        patcher = mock.patch.object(cxs, 'yaml', cxs.yaml if self.use_yaml else None)
        patcher.start()
        self.addCleanup(patcher.stop)

        tmp = tempfile.mkdtemp(prefix='csr-xray-test-')
        self.addCleanup(shutil.rmtree, tmp)
        self.base = Path(tmp) / 'services'
        (self.base / 'prometheus').mkdir(parents=True)
        (self.base / 'xray').mkdir()
        self.compose_file = self.base / 'docker-compose.yml'
        self.prometheus_file = self.base / 'prometheus' / 'prometheus.yml'
        shutil.copyfile(str(self.compose_source), str(self.compose_file))
        shutil.copyfile(str(self.prometheus_source), str(self.prometheus_file))
        self.public_file = self.base / 'xray' / 'public-proxies.txt'
        shutil.copyfile(str(REPO_SERVICES_DIR / 'xray' / 'public-proxies.txt'), str(self.public_file))
        self.creator = XrayServiceCreator(base_dir=str(self.base))

    def snapshot(self):
        tree = sorted(str(p.relative_to(self.base)) for p in (self.base / 'xray').rglob('*'))
        return read(self.compose_file), read(self.prometheus_file), tree, read(self.public_file)

    def create(self, name, http_port, socks_port, url=VLESS_URL, private=False):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.creator.create_service(url, name, http_port, socks_port, private=private)
        return out.getvalue()

    def assertRejected(self, name, http_port, socks_port, message, exc=ValueError, private=False):
        before = self.snapshot()
        with self.assertRaises(exc) as ctx:
            self.create(name, http_port, socks_port, private=private)
        self.assertIn(message, str(ctx.exception))
        self.assertEqual(self.snapshot(), before, "拒绝时不能修改任何文件")

    def assertOnlyNewV2rayTarget(self, old_text, new_text, name):
        """
        新目标只能出现在 v2ray job 的 targets 中 (不能跑到后面的 det-master job)，
        且 prometheus.yml 除这一行外没有其他变化。不依赖被测脚本自己的校验。
        """
        target = f'xray-{name}-exporter:9550'
        line = f'          - "{target}"'
        new_lines = new_text.splitlines()
        self.assertEqual(new_lines.count(line), 1, line)
        index = new_lines.index(line)
        v2ray = new_lines.index('  - job_name: "v2ray"')
        next_job = next((i for i in range(v2ray + 1, len(new_lines)) if JOB_RE.match(new_lines[i])), len(new_lines))
        self.assertTrue(v2ray < index < next_job, "新目标不在 v2ray job 中")
        self.assertEqual(new_lines[:index] + new_lines[index + 1:], old_text.splitlines())
        self.assertEqual(len(new_text) - len(old_text), len(line) + 1)
        if pyyaml is None:
            return
        old, new = pyyaml.safe_load(old_text), pyyaml.safe_load(new_text)
        jobs = {job['job_name']: job for job in new['scrape_configs']}
        for job_name, job in jobs.items():
            targets = [t for sc in job.get('static_configs') or [] for t in sc.get('targets') or []]
            if job_name == 'v2ray':
                # The xray proxies are the first group; the HA entry, when present, is another.
                self.assertEqual(job['static_configs'][0]['targets'][-1], target)
                job['static_configs'][0]['targets'].remove(target)
            else:
                self.assertNotIn(target, targets, job_name)
        self.assertEqual(new, old)


class PortCheckYamlTest(_TempServicesMixin, unittest.TestCase):

    def test_compose_services(self):
        services = compose_services(read(self.compose_file))
        self.assertEqual(list(services), [
            'hbbs', 'wandb', 'portainer', 'frp', 'nginx', 'flare',
            'xray-usca5-bwh-sla-1tb', 'xray-usca5-bwh-sla-1tb-exporter',
            'xray-miyaip-jp-01_relay_jpty1-bwh-2tb', 'xray-miyaip-jp-01_relay_jpty1-bwh-2tb-exporter',
            'grafana', 'grafana-renderer', 'csr-port-forms', 'nextcloud-nginx',
        ])

    def test_host_port_owners(self):
        owners = host_port_owners(compose_services(read(self.compose_file)))
        expected = {
            80: 'nginx', 443: 'nginx', 33322: 'nginx', 8080: 'nginx', 8081: 'wandb', 9000: 'portainer',
            5005: 'flare', 10080: 'grafana', 8008: 'nextcloud-nginx',
            59880: 'xray-usca5-bwh-sla-1tb', 59889: 'xray-usca5-bwh-sla-1tb',
            52089: 'xray-miyaip-jp-01_relay_jpty1-bwh-2tb', 52889: 'xray-miyaip-jp-01_relay_jpty1-bwh-2tb',
            6100: 'csr-port-forms', 6200: 'csr-port-forms', 6300: 'csr-port-forms', 6301: 'csr-port-forms',
            6302: 'csr-port-forms', 6400: 'csr-port-forms', 6600: 'csr-port-forms', 6700: 'csr-port-forms',
            6800: 'csr-port-forms',
        }
        for port, owner in expected.items():
            self.assertEqual(owners.get(port), owner, port)
        for port in (7000, 7500, 9100, 21115, 21116, 21117, 21118, 21119, 50000):
            self.assertIn(port, owners)
        # 只比较主机侧: 容器端口和随机主机端口不算占用
        for port in (1089, 8889, 10085, 9550, 3000, 22, 53, 6500, 6550, 7300):
            self.assertNotIn(port, owners)

    def test_check_port_available(self):
        self.assertEqual(self.creator.check_port_available(443), (False, 'nginx'))
        self.assertEqual(self.creator.check_port_available(59889), (False, 'xray-usca5-bwh-sla-1tb'))
        self.assertFalse(self.creator.check_port_available(7500)[0])
        self.assertEqual(self.creator.check_port_available(56889), (True, None))

    def test_rejects_used_ports(self):
        for port, owner in [(80, 'nginx'), (443, 'nginx'), (8080, 'nginx'), (8081, 'wandb'),
                            (59889, 'xray-usca5-bwh-sla-1tb'), (52089, 'xray-miyaip-jp-01_relay_jpty1-bwh-2tb'),
                            (6600, 'csr-port-forms'), (7000, 'frps'), (9100, 'node-exporter'),
                            (21116, 'rustdesk'), (50000, 'Harbor')]:
            with self.subTest(port=port):
                self.assertRejected('csr-new', port, 56089, owner)
                self.assertRejected('csr-new', 56889, port, owner)

    def test_rejects_existing_names(self):
        for name in ('usca5-bwh-sla-1tb', 'miyaip-jp-01_relay_jpty1-bwh-2tb',
                     'usca5-bwh-sla-1tb-exporter'):  # 与已有服务的 exporter 同名
            with self.subTest(name=name):
                self.assertRejected(name, 56889, 56089, '已存在')

    def test_rejects_existing_directory(self):
        config_dir = self.base / 'xray' / 'csr-stale' / 'config'
        config_dir.mkdir(parents=True)
        (config_dir / 'config.json').write_text('{"marker": true}', encoding='utf-8')
        self.assertRejected('csr-stale', 56889, 56089, '目录已存在')
        self.assertEqual(read(config_dir / 'config.json'), '{"marker": true}')

    def test_rejects_invalid_names(self):
        for name in ('', 'Upper', 'a b', '../evil', 'a/b', 'a:b', '-lead', '_lead', 'jp.tokyo', 'caf\u00e9', 'trailing\n'):
            with self.subTest(name=name):
                self.assertRejected(name, 56889, 56089, '服务名称无效')

    def test_rejects_invalid_ports(self):
        for http_port, socks_port, message in [(0, 56089, '端口无效'), (56889, 70000, '端口无效'),
                                               (-1, 56089, '端口无效'), (56889, 56889, '不能相同')]:
            with self.subTest(ports=(http_port, socks_port)):
                self.assertRejected('csr-new', http_port, socks_port, message)

    def test_rejects_bad_share_url_before_writing(self):
        before = self.snapshot()
        with self.assertRaises(ValueError) as ctx:
            self.create('csr-new', 56889, 56089, url='vless://x@noport.example.com#x')
        self.assertIn('缺少端口', str(ctx.exception))
        self.assertEqual(self.snapshot(), before)


class PortCheckRegexTest(PortCheckYamlTest):
    use_yaml = False


class CreateServiceYamlTest(_TempServicesMixin, unittest.TestCase):

    def test_create_service_with_underscore_name(self):
        name = 'csr_new-node_01'
        old_compose, old_prometheus, _, old_public = self.snapshot()
        output = self.create(name, 56889, 56089)

        # 两个服务块与旧模板逐字一致，插在最后一个 xray exporter (名字带 '_') 之后
        self.assertEqual(read(self.compose_file),
                         old_compose.replace('  grafana:\n', golden_block(name) + '  grafana:\n', 1))
        anchor = '          - "xray-miyaip-jp-01_relay_jpty1-bwh-2tb-exporter:9550"\n'
        self.assertEqual(read(self.prometheus_file),
                         old_prometheus.replace(anchor, anchor + f'          - "xray-{name}-exporter:9550"\n', 1))
        self.assertOnlyNewV2rayTarget(old_prometheus, read(self.prometheus_file), name)
        # config.json 与模板 (旧生成器为同一链接写入的文件) 逐字节一致
        self.assertEqual(read(self.base / 'xray' / name / 'config' / 'config.json'),
                         read(FIXTURES_DIR / CREATE_SERVICE_TEMPLATE))
        self.assertTrue((self.base / 'xray' / name / 'log').is_dir())
        self.assertIn(f'docker compose up -d xray-{name} xray-{name}-exporter', output)
        self.assertIn('docker compose kill -s SIGHUP prometheus', output)
        self.assertIn('curl -x http://localhost:56889', output)
        self.assertNotIn('docker-compose ', output)
        # a public proxy: added to the public proxies
        self.assertEqual(read(self.public_file), old_public + name + '\n')

        # 再次运行同名服务: 拒绝，文件不变
        self.assertRejected(name, 57889, 57089, '已存在')

    def test_second_service_goes_after_the_first(self):
        self.create('csr-first', 56889, 56089)
        self.create('csr_second', 41889, 41089)
        compose = read(self.compose_file)
        self.assertLess(compose.index('  xray-csr-first-exporter:\n'), compose.index('  xray-csr_second:\n'))
        self.assertLess(compose.index('  xray-csr_second-exporter:\n'), compose.index('  grafana:\n'))
        if cxs.yaml is not None:
            services = cxs.yaml.safe_load(compose)['services']
            self.assertEqual(services['xray-csr_second']['ports'], ['41089:1089', '41889:8889'])

    def _fail_on_write(self, failing_path):
        real_write = XrayServiceCreator._write_text
        state = {'failed': False}

        def fake_write(path, text, **kwargs):
            if Path(path) == failing_path and not state['failed']:
                state['failed'] = True
                # 模拟写到一半失败: 文件已被截断
                real_write(path, text[:10], **kwargs)
                raise OSError('模拟写入失败')
            real_write(path, text, **kwargs)

        return mock.patch.object(XrayServiceCreator, '_write_text', staticmethod(fake_write))

    def test_failure_writing_prometheus_restores_everything(self):
        before = self.snapshot()
        with self._fail_on_write(self.prometheus_file):
            with self.assertRaises(OSError):
                self.create('csr-rollback', 56889, 56089)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.base / 'xray' / 'csr-rollback').exists())

    def test_failure_writing_compose_restores_everything(self):
        before = self.snapshot()
        with self._fail_on_write(self.compose_file):
            with self.assertRaises(OSError):
                self.create('csr-rollback', 56889, 56089)
        self.assertEqual(self.snapshot(), before)

    def test_failure_writing_public_proxies_restores_everything(self):
        before = self.snapshot()
        with self._fail_on_write(self.public_file):
            with self.assertRaises(OSError):
                self.create('csr-rollback', 56889, 56089)
        self.assertEqual(self.snapshot(), before)

    def test_failure_writing_config_restores_everything(self):
        before = self.snapshot()
        with self._fail_on_write(self.base / 'xray' / 'csr-rollback' / 'config' / 'config.json'):
            with self.assertRaises(OSError):
                self.create('csr-rollback', 56889, 56089)
        self.assertEqual(self.snapshot(), before)

    def test_class_is_required(self):
        with self.assertRaises(TypeError):
            self.creator.create_service(VLESS_URL, 'csr-x', 56889, 56089)
        for private in (None, 0, 'yes'):
            self.assertRejected('csr-x', 56889, 56089, '--public', private=private)
        for argv in ([], ['--public', '--private']):
            with mock.patch.object(sys, 'argv', ['create_xray_service.py', VLESS_URL, 'csr-x', '56889', '56089',
                                                 '--base-dir', str(self.base)] + argv), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
                cxs.main()
            self.assertEqual(ctx.exception.code, 2)
        self.assertFalse((self.base / 'xray' / 'csr-x').exists())

    def test_private_proxy_requires_admin_accounts(self):
        self.write_accounts(False, {'alice': 'pw-a', 'boss': 'pw-b', 'svc-ha': 'pw-h', 'svc-monitor': 'pw-m'})
        data = json.loads(read(self.base / 'proxy-accounts' / 'accounts.json'))
        data = {'version': 3, 'raw_auth': False, 'accounts': {
            name: {'password': pw, 'enabled': True, 'admin': name == 'boss'} for name, pw in data['accounts'].items()}}
        (self.base / 'proxy-accounts' / 'accounts.json').write_text(json.dumps(data))
        public = read(self.public_file)
        output = self.create('csr-private', 56889, 56089, private=True)
        path = self.base / 'xray' / 'csr-private' / 'config' / 'config.json'
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        config = json.loads(read(path))
        users = [{'user': 'boss', 'pass': 'pw-b'}, {'user': 'svc-ha', 'pass': 'pw-h'},
                 {'user': 'svc-monitor', 'pass': 'pw-m'}]
        for inbound in config['inbounds'][:2]:
            self.assertEqual(inbound['settings']['users'], users)
        self.assertEqual(read(self.public_file), public)  # not listed: private
        self.assertIn('ha-private', output)
        self.assertNotIn('pw-', output)

    def test_private_proxy_without_service_accounts_rejected(self):
        self.write_accounts(False, {'alice': 'pw-a', 'svc-monitor': 'pw-m'})
        self.assertRejected('csr-private', 56889, 56089, 'svc-ha', private=True)

    def test_private_proxy_listed_as_public_rejected(self):
        self.write_accounts(False, {'svc-ha': 'pw-h', 'svc-monitor': 'pw-m'})
        self.public_file.write_text(read(self.public_file) + 'csr-listed\n')
        self.assertRejected('csr-listed', 56889, 56089, 'public-proxies.txt', private=True)

    def test_missing_public_proxies_file_rejected(self):
        self.public_file.unlink()
        before = (read(self.compose_file), read(self.prometheus_file))
        with self.assertRaises(FileNotFoundError):
            self.create('csr-x', 56889, 56089)
        self.assertEqual((read(self.compose_file), read(self.prometheus_file)), before)

    def write_accounts(self, raw_auth, accounts):
        path = self.base / 'proxy-accounts' / 'accounts.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({'raw_auth': raw_auth, 'accounts': accounts}))

    def test_raw_auth_off_keeps_template(self):
        self.write_accounts(False, {'alice': 'pw-a'})
        self.create('csr-noauth', 56889, 56089)
        self.assertEqual(read(self.base / 'xray' / 'csr-noauth' / 'config' / 'config.json'),
                         read(FIXTURES_DIR / CREATE_SERVICE_TEMPLATE))

    def test_raw_auth_on_requires_accounts(self):
        self.write_accounts(True, {'alice': 'pw-a', 'svc-ha': 'pw-h'})
        output = self.create('csr-auth', 56889, 56089)
        path = self.base / 'xray' / 'csr-auth' / 'config' / 'config.json'
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        config = json.loads(read(path))
        users = [{'user': 'alice', 'pass': 'pw-a'}, {'user': 'svc-ha', 'pass': 'pw-h'}]
        for inbound in config['inbounds'][:2]:
            self.assertEqual(inbound['settings']['users'], users)
        self.assertEqual(config['inbounds'][1]['settings']['auth'], 'password')
        # Apart from the users, the template
        XrayConfigHandler.set_inbound_users(config, None)
        self.assertEqual(json.dumps(config, indent=4, ensure_ascii=False), read(FIXTURES_DIR / CREATE_SERVICE_TEMPLATE))
        self.assertNotIn('pw-a', output)

    def test_raw_auth_on_open_proxy_keeps_template(self):
        """A public proxy listed in open-proxies.txt never requires accounts, also with raw auth on."""
        self.write_accounts(True, {'alice': 'pw-a', 'svc-ha': 'pw-h'})
        (self.base / 'xray' / 'open-proxies.txt').write_text('csr-open\n')
        self.create('csr-open', 56889, 56089)
        self.assertEqual(read(self.base / 'xray' / 'csr-open' / 'config' / 'config.json'),
                         read(FIXTURES_DIR / CREATE_SERVICE_TEMPLATE))

    def test_private_proxy_listed_as_open_rejected(self):
        self.write_accounts(False, {'svc-ha': 'pw-h', 'svc-monitor': 'pw-m'})
        (self.base / 'xray' / 'open-proxies.txt').write_text('csr-open\n')
        self.assertRejected('csr-open', 56889, 56089, 'open-proxies.txt', private=True)

    def test_raw_auth_on_without_accounts_rejected(self):
        self.write_accounts(True, {})
        self.assertRejected('csr-auth', 56889, 56089, '没有账号')

    def test_restore_keeps_prometheus_inode(self):
        inode = self.prometheus_file.stat().st_ino
        self.create('csr-inode', 56889, 56089)
        self.assertEqual(self.prometheus_file.stat().st_ino, inode)

    def _rewrite_prometheus(self, old, new):
        text = read(self.prometheus_file)
        self.assertIn(old, text)
        self.prometheus_file.write_text(text.replace(old, new, 1), encoding='utf-8')

    def test_rejects_v2ray_job_with_labels_first(self):
        # 以前: 非贪婪匹配跨进 det-master job，生成无效的 YAML 并报告成功
        self._rewrite_prometheus('    static_configs:\n      - targets:\n          - "xray-usca5',
                                 '    static_configs:\n      - labels:\n          group: xray\n'
                                 '        targets:\n          - "xray-usca5')
        self.assertRejected('csr-new', 56889, 56089, '格式与预期不同')

    def test_rejects_v2ray_job_with_flow_list(self):
        self._rewrite_prometheus(
            '      - targets:\n          - "xray-usca5-bwh-sla-1tb-exporter:9550"\n'
            '          - "xray-miyaip-jp-01_relay_jpty1-bwh-2tb-exporter:9550"\n',
            '      - targets: ["xray-usca5-bwh-sla-1tb-exporter:9550"]\n')
        self.assertRejected('csr-new', 56889, 56089, '格式与预期不同')

    def test_rejects_unquoted_job_name_before_touching_compose(self):
        self._rewrite_prometheus('- job_name: "v2ray"', '- job_name: v2ray')
        self.assertRejected('csr-new', 56889, 56089, '无法找到 v2ray job')

    def test_det_master_credentials_file_untouched(self):
        # PR #3: v2ray 后面是带 relabel_configs 的 det-master job，文件以 authorization.credentials_file 结尾
        old_prometheus = read(self.prometheus_file)
        self.assertTrue(old_prometheus.endswith(METRICS_TOKEN_AUTH))
        self.create('csr_det-tail', 56889, 56089)
        new_prometheus = read(self.prometheus_file)
        self.assertTrue(new_prometheus.endswith(METRICS_TOKEN_AUTH))
        self.assertOnlyNewV2rayTarget(old_prometheus, new_prometheus, 'csr_det-tail')
        if pyyaml is not None:
            jobs = {job['job_name']: job for job in pyyaml.safe_load(new_prometheus)['scrape_configs']}
            self.assertEqual(jobs['det-master']['authorization'],
                             {'type': 'Bearer', 'credentials_file': '/run/determined-metrics/token'})
            self.assertEqual(jobs['det-master']['static_configs'], [{'targets': ['cvglcorevm.lan:8080']}])

    def test_old_bearer_token_shape(self):
        # PR #3 之前服务器上的形状: 旧 watchdog 把 bearer_token 写在 det-master 的最后一行
        self._rewrite_prometheus(METRICS_TOKEN_AUTH, '    bearer_token: "placeholder-not-a-token"\n')
        old_prometheus = read(self.prometheus_file)
        self.create('csr-new', 56889, 56089)
        lines = read(self.prometheus_file).splitlines()
        self.assertEqual(lines[-1], '    bearer_token: "placeholder-not-a-token"')
        self.assertIn('          - "xray-csr-new-exporter:9550"', lines)
        self.assertOnlyNewV2rayTarget(old_prometheus, read(self.prometheus_file), 'csr-new')

    def test_existing_prometheus_target_is_kept(self):
        anchor = '          - "xray-usca5-bwh-sla-1tb-exporter:9550"\n'
        self._rewrite_prometheus(anchor, anchor + '          - "xray-csr-stale-exporter:9550"\n')
        old_prometheus = read(self.prometheus_file)
        output = self.create('csr-stale', 56889, 56089)
        self.assertIn('已存在 xray-csr-stale-exporter', output)
        self.assertEqual(read(self.prometheus_file), old_prometheus)
        self.assertIn('  xray-csr-stale:\n', read(self.compose_file))

    def test_rejects_insert_under_top_level_key(self):
        # 以前: 新服务被插入到 services 之后的顶层键 (networks:) 下面
        self.compose_file.write_text(
            'services:\n'
            '  web:\n'
            '    image: nginx\n'
            '    ports:\n'
            '      - "80:80"\n'
            '\n'
            '  xray-a:\n'
            '    image: teddysun/xray:26.11.9@sha256:b3e117b834878d1e12f91e7b16270db104c2f0e5f0effbc0ece9088c473b5393\n'
            '    ports:\n'
            '      - 30089:1089\n'
            '      - 38889:8889\n'
            '\n'
            '  xray-a-exporter:\n'
            '    image: wi1dcard/v2ray-exporter:master\n'
            '    expose:\n'
            '      - 9550\n'
            '\n'
            'networks:\n'
            '  grafana_monitor:\n'
            '    driver: bridge\n', encoding='utf-8')
        self.assertRejected('csr-new', 56889, 56089, '缺少')


class CreateServiceRegexTest(CreateServiceYamlTest):
    use_yaml = False


@unittest.skipUnless((REPO_SERVICES_DIR / 'docker-compose.yml').exists()
                     and (REPO_SERVICES_DIR / 'prometheus' / 'prometheus.yml').exists(),
                     "仓库中的 services/docker-compose.yml 或 prometheus.yml 不存在")
class RepoFilesYamlTest(_TempServicesMixin, unittest.TestCase):
    """在当前仓库 services/docker-compose.yml 和 prometheus.yml 的副本上运行"""

    compose_source = REPO_SERVICES_DIR / 'docker-compose.yml'
    prometheus_source = REPO_SERVICES_DIR / 'prometheus' / 'prometheus.yml'

    def test_create_after_last_exporter(self):
        old_compose, old_prometheus, _, _ = self.snapshot()
        owners = host_port_owners(compose_services(old_compose))
        http_port, socks_port = next(pair for pair in [(56889, 56089), (41889, 41089), (46889, 46089)]
                                     if not set(pair) & set(owners))
        name = 'csr_repo-check_01'
        self.create(name, http_port, socks_port)

        old_lines = old_compose.splitlines(True)
        last = max(i for i, line in enumerate(old_lines) if EXPORTER_RE.match(line))
        insert_at = next(i for i in range(last + 1, len(old_lines)) if re.match(r'^\s{2}[a-zA-Z0-9]', old_lines[i]))
        block = golden_block(name).replace('56089:', f'{socks_port}:').replace('56889:', f'{http_port}:')
        self.assertEqual(read(self.compose_file), ''.join(old_lines[:insert_at]) + block + ''.join(old_lines[insert_at:]))
        # 新目标紧跟在 v2ray job 第一组 targets (proxy_role: raw 的 xray 代理) 的最后一个之后，
        # 后面的 HA 入口组和其他 job 逐字不变
        old_prom_lines = old_prometheus.splitlines(True)
        last_target = next(i for i, line in enumerate(old_prom_lines) if V2RAY_TARGET_RE.match(line.rstrip('\n')))
        while V2RAY_TARGET_RE.match(old_prom_lines[last_target + 1].rstrip('\n')):
            last_target += 1
        new_line = f'          - "xray-{name}-exporter:9550"\n'
        self.assertEqual(read(self.prometheus_file),
                         ''.join(old_prom_lines[:last_target + 1]) + new_line + ''.join(old_prom_lines[last_target + 1:]))
        self.assertOnlyNewV2rayTarget(old_prometheus, read(self.prometheus_file), name)
        if pyyaml is not None:
            jobs = {job['job_name']: job for job in pyyaml.safe_load(read(self.prometheus_file))['scrape_configs']}
            groups = {target: group.get('labels', {}).get('proxy_role')
                      for group in jobs['v2ray']['static_configs'] for target in group['targets']}
            self.assertEqual(groups[f'xray-{name}-exporter:9550'], 'raw')
            self.assertEqual(groups['xray-ha-exporter:9550'], 'entry')

    def test_rejects_ports_and_names_in_use(self):
        services = compose_services(read(self.compose_file))
        owners = host_port_owners(services)
        for port in (80, 443, 8080):
            self.assertIn(port, owners)
        for port in sorted(p for p in owners if p not in cxs.RESERVED_HOST_PORTS)[:12]:
            with self.subTest(port=port):
                self.assertRejected('csr-new', port, 56089 if port != 56089 else 41089, '占用')
        names = [m.group(1) for m in (re.match(r'^xray-(.+)-exporter$', s) for s in services) if m]
        self.assertTrue(any('_' in n for n in names), "当前 compose 中应有带 '_' 的服务名")
        for name in names:
            with self.subTest(name=name):
                self.assertRejected(name, 56889, 56089, '已存在')


    def test_ha_entry_ports(self):
        # ha-general on 30089/30889 and ha-private on 31089/31889: x089 SOCKS5 and x889 HTTP, below the Linux
        # ephemeral range (32768-60999), each published once and by xray-ha only, and not reserved
        services = compose_services(read(self.compose_file))
        for port in (30089, 30889, 31089, 31889):
            with self.subTest(port=port):
                publishers = [name for name, entries in services.items()
                              for entry in entries if port in published_host_ports(entry)]
                self.assertEqual(publishers, ['xray-ha'])
                self.assertNotIn(port, cxs.RESERVED_HOST_PORTS)
                self.assertLess(port, 32768)
                self.assertRejected('csr-new', port, 56089, 'xray-ha')


class RepoFilesRegexTest(RepoFilesYamlTest):
    use_yaml = False


class AccountsLockTest(_TempServicesMixin, unittest.TestCase):
    """新代理从读取账号到写入配置都持有 proxy-accounts/.lock：同时进行的 disable + render 不会留下旧账号"""

    def test_concurrent_disable_does_not_leave_the_user_on_the_new_proxy(self):
        self.concurrent_change(private=False, user='alice', change=lambda accounts: accounts.set_enabled('alice', False))

    def test_concurrent_admin_off_does_not_leave_the_admin_on_the_new_private_proxy(self):
        self.concurrent_change(private=True, user='boss', change=lambda accounts: accounts.set_admin('boss', False))

    def concurrent_change(self, private, user, change):
        import proxy_accounts as pa
        for rel in (pa.HA_EXAMPLE, pa.BLACKBOX_EXAMPLE):
            (self.base / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REPO_SERVICES_DIR / rel, self.base / rel)
        accounts = pa.Accounts(self.base)
        accounts.raw_auth = not private  # a private proxy requires its admins either way
        for name in ('alice', 'bob', 'boss', 'carol', 'svc-ha', 'svc-monitor'):
            accounts.accounts[name] = pa.new_password()
        accounts.admins |= {'boss', 'carol'}
        accounts.save()

        def disable_and_render():
            # What "proxy_accounts.py disable alice" (or "admin boss off") and then "render" do, without their output
            with pa.accounts_lock(self.base):
                accounts = pa.Accounts(self.base)
                change(accounts)
                accounts.save()
            with pa.accounts_lock(self.base):
                codes.append(len(pa.render(pa.Accounts(self.base))))
            done.set()

        codes, done, during = [], threading.Event(), []
        other = threading.Thread(target=disable_and_render)
        parse = XrayConfigHandler.parse_share_url

        def parse_while_another_disables(url):
            # The accounts were read; another admin disables alice and renders now
            other.start()
            during.append(done.wait(1.0))
            return parse(url)

        with mock.patch.object(cxs.XrayConfigHandler, 'parse_share_url', side_effect=parse_while_another_disables):
            self.create('csr-locked', 56889, 56089, private=private)
        other.join(10)
        self.assertEqual(len(codes), 1)
        config = json.loads(read(self.base / 'xray' / 'csr-locked' / 'config' / 'config.json'))
        for inbound in config['inbounds'][:2]:
            users = [u['user'] for u in inbound['settings']['users']]
            self.assertNotIn(user, users)
            self.assertIn('carol' if private else 'bob', users)
        self.assertEqual(during, [False])  # the disable waited for the new proxy


class XrayImageTest(unittest.TestCase):
    """每个代理与 xray-ha 使用同一个固定版本和 digest 的镜像，生成器也是 (应用配置前用 xray-ha 的镜像检查)"""

    def test_generator_and_repo_proxies_use_the_ha_image(self):
        self.assertRegex(cxs.XRAY_IMAGE, r'^teddysun/xray:[0-9.]+@sha256:[0-9a-f]{64}$')
        compose = read(REPO_SERVICES_DIR / 'docker-compose.yml')
        images = {}
        current = None
        for line in compose.splitlines():
            match = re.match(r'^  ([A-Za-z0-9._-]+):\s*$', line)
            if match:
                current = match.group(1)
            match = re.match(r'^    image:\s*(\S+)\s*$', line)
            if match and current:
                images[current] = match.group(1)
        self.assertEqual(images['xray-ha'], cxs.XRAY_IMAGE)
        proxies = {name: image for name, image in images.items()
                   if name.startswith('xray-') and not name.endswith('-exporter')}
        self.assertGreater(len(proxies), 1)
        self.assertEqual({name: cxs.XRAY_IMAGE for name in proxies}, proxies)
        self.assertNotIn('teddysun/xray:latest', compose)


class DefaultBaseDirTest(unittest.TestCase):
    """
    不带 --base-dir、以相对路径运行脚本时，也要找到脚本所在的 services/ 目录。
    Python 3.8 (附属服务 VM) 下 __main__ 的 __file__ 是命令行上输入的相对路径 (3.9 起为绝对路径)，
    所以只有在 Python 3.8 下运行这个测试才能发现问题。
    """

    NAME = 'csr-cwd'

    def run_script(self, cwd_parts, script):
        tmp = tempfile.mkdtemp(prefix='csr-xray-cwd-test-')
        self.addCleanup(shutil.rmtree, tmp)
        base = Path(tmp) / 'services'
        scripts = base / 'xray' / 'scripts'
        scripts.mkdir(parents=True)
        (base / 'prometheus').mkdir()
        for name in ('create_xray_service.py', 'XrayConfigHandler.py', 'proxy_accounts.py', 'line_ha.py'):
            shutil.copyfile(str(SCRIPTS_DIR / name), str(scripts / name))
        shutil.copyfile(str(FIXTURES_DIR / 'compose_sample.yml'), str(base / 'docker-compose.yml'))
        shutil.copyfile(str(FIXTURES_DIR / 'prometheus_sample.yml'), str(base / 'prometheus' / 'prometheus.yml'))
        shutil.copyfile(str(REPO_SERVICES_DIR / 'xray' / 'public-proxies.txt'),
                        str(base / 'xray' / 'public-proxies.txt'))
        result = subprocess.run(
            [sys.executable, script, VLESS_URL, self.NAME, '56889', '56089', '--public'],
            cwd=str(base.joinpath(*cwd_parts)), env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120,
        )
        return base, result

    def test_relative_invocations_find_services_dir(self):
        cases = [
            (('xray', 'scripts'), 'create_xray_service.py'),     # 在 scripts/ 下运行
            (('xray',), 'scripts/create_xray_service.py'),       # 在 xray/ 下运行 (xray/README.md 的写法)
            ((), 'xray/scripts/create_xray_service.py'),         # 在 services/ 下运行 (scripts/README.md 的写法)
        ]
        for cwd_parts, script in cases:
            with self.subTest(cwd='/'.join(('services',) + cwd_parts), script=script):
                base, result = self.run_script(cwd_parts, script)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'  xray-{self.NAME}-exporter:\n', read(base / 'docker-compose.yml'))
                self.assertIn(f'          - "xray-{self.NAME}-exporter:9550"\n',
                              read(base / 'prometheus' / 'prometheus.yml'))
                self.assertTrue((base / 'xray' / self.NAME / 'config' / 'config.json').is_file())
                self.assertEqual(sorted(p.name for p in (base / 'xray').iterdir()),
                                 sorted([self.NAME, 'public-proxies.txt', 'scripts']))


if __name__ == '__main__':
    unittest.main()
