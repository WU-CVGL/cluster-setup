# -*- coding: utf-8 -*-
"""
XrayConfigHandler: 分享链接语料 (fixtures/cases.json) 的解析/转换结果，以及 config.json 模板的逐字节比较。

运行 (Python 3.8+，不需要第三方包):
    cd services/xray/scripts && python3 -B -m unittest discover -s tests -v
"""

import copy
import json
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from corpus import (  # noqa: E402
    CREATE_SERVICE_TEMPLATE, VLESS_TO_CONFIG_TEMPLATE, dumps_config, expected_config, load_cases, read_fixture,
)
from XrayConfigHandler import RESERVED_OUTBOUND_TAGS, XrayConfigHandler  # noqa: E402
from create_xray_service import XrayServiceCreator  # noqa: E402


def dumps(obj):
    # 不排序键: 键的顺序也是输出的一部分
    return json.dumps(obj, ensure_ascii=False)


def convert(url):
    return XrayConfigHandler.to_xray_outbound(XrayConfigHandler.parse_share_url(url))


class CorpusTest(unittest.TestCase):
    """10 类分享链接 + 各种边界情况，输出必须与 cases.json 完全一致"""

    @classmethod
    def setUpClass(cls):
        cls.cases = load_cases()
        cls.ok_cases = [case for case in cls.cases if 'error' not in case]
        cls.error_cases = [case for case in cls.cases if 'error' in case]

    def test_corpus_covers_required_link_types(self):
        names = {case['name'] for case in self.cases}
        for required in ('vmess_ws_tls', 'vmess_grpc', 'vmess_tcp', 'vless_reality_vision', 'vless_ws_tls',
                         'vless_grpc', 'trojan_tls', 'trojan_ws', 'ss_sip002_b64', 'ss_legacy_b64'):
            self.assertIn(required, names)

    def test_cases_well_formed(self):
        names = [case['name'] for case in self.cases]
        self.assertEqual(len(names), len(set(names)))
        for case in self.cases:
            with self.subTest(case['name']):
                keys = {'error'} if 'error' in case else {'parse', 'outbound'}
                self.assertEqual(set(case), {'name', 'note', 'url'} | keys)
        # 防止误删用例
        self.assertGreaterEqual(len(self.ok_cases), 51)
        self.assertGreaterEqual(len(self.error_cases), 11)

    def test_parse_share_url(self):
        for case in self.ok_cases:
            with self.subTest(case['name']):
                self.assertEqual(XrayConfigHandler.parse_share_url(case['url']), case['parse'])

    def test_to_xray_outbound(self):
        for case in self.ok_cases:
            with self.subTest(case['name']):
                self.assertEqual(dumps(convert(case['url'])), dumps(case['outbound']))

    def test_errors(self):
        for case in self.error_cases:
            with self.subTest(case['name']):
                with self.assertRaises(ValueError) as ctx:
                    convert(case['url'])
                self.assertIn(case['error'], str(ctx.exception))

    def test_outbound_tag_never_reserved(self):
        for case in self.ok_cases:
            with self.subTest(case['name']):
                self.assertNotIn(convert(case['url'])['tag'], RESERVED_OUTBOUND_TAGS)


class TemplateTest(unittest.TestCase):
    """每个用例的 config.json 与模板 (只替换 outbounds[0]) 逐字节一致；outbound 原样插入"""

    @classmethod
    def setUpClass(cls):
        cls.cases = [case for case in load_cases() if 'error' not in case]

    def test_templates_are_canonical(self):
        # expected_config 重新序列化模板，所以模板必须就是 json.dumps(indent=4) 的输出
        text = read_fixture(CREATE_SERVICE_TEMPLATE)
        self.assertEqual(dumps_config(json.loads(text)), text)
        text = read_fixture(VLESS_TO_CONFIG_TEMPLATE)
        self.assertEqual(dumps_config(json.loads(text)) + "\n", text)

    def test_build_xray_config_matches_template(self):
        for case in self.cases:
            with self.subTest(case['name']):
                config = XrayConfigHandler.build_xray_config(
                    case['outbound'], include_stats_api=True, loglevel="none",
                    access_log="/var/log/xray/access.log", error_log="/var/log/xray/error.log",
                )
                self.assertEqual(dumps_config(config), expected_config(CREATE_SERVICE_TEMPLATE, case['outbound']))

    def test_generate_xray_config_matches_template(self):
        creator = XrayServiceCreator(base_dir="/nonexistent")
        for case in self.cases:
            with self.subTest(case['name']):
                config = creator.generate_xray_config(case['outbound'], 12345, 23456, "x")
                self.assertEqual(dumps_config(config), expected_config(CREATE_SERVICE_TEMPLATE, case['outbound']))

    def test_outbound_inserted_verbatim(self):
        # 模板不检查、不修改 outbound: 保留 tag、端口范围、vmess level 都由 to_xray_outbound 负责。
        # 用 to_xray_outbound 不会产生的 outbound (重构前的输出形式) 确认这一点
        raws = [
            {"protocol": "vmess", "sendThrough": "0.0.0.0",
             "settings": {"vnext": [{"address": "x.example.com", "port": 0,
                                     "users": [{"id": "u", "alterId": 0, "security": "auto", "level": 2}]}]},
             "streamSettings": {}, "tag": "api"},
            {"protocol": "shadowsocks", "sendThrough": "0.0.0.0",
             "settings": {"servers": [{"address": "x.example.com", "port": 70000, "method": "aes-256-gcm",
                                       "password": "p", "email": ""}]},
             "streamSettings": {}, "tag": "DIRECT"},
            {"protocol": "trojan", "sendThrough": "0.0.0.0",
             "settings": {"servers": [{"address": "x.example.com", "port": 443, "password": "p", "email": ""}]},
             "streamSettings": {"network": "tcp", "security": "tls"}, "tag": "BLACKHOLE"},
        ]
        creator = XrayServiceCreator(base_dir="/nonexistent")
        for raw in raws:
            with self.subTest(raw["tag"]):
                for config in (XrayConfigHandler.build_xray_config(copy.deepcopy(raw)),
                               creator.generate_xray_config(copy.deepcopy(raw), 12345, 23456, "x")):
                    self.assertEqual(dumps(config["outbounds"][0]), dumps(raw))

    def test_default_template_has_no_stats_api(self):
        for case in self.cases:
            with self.subTest(case['name']):
                config = XrayConfigHandler.build_xray_config(case['outbound'])
                self.assertEqual(dumps_config(config), expected_config(VLESS_TO_CONFIG_TEMPLATE, case['outbound']))
        config = XrayConfigHandler.build_xray_config(self.cases[0]['outbound'])
        self.assertEqual(list(config),["log", "dns", "inbounds", "outbounds", "routing"])
        self.assertEqual(config["log"], {"loglevel": "warning"})
        self.assertEqual([i["tag"] for i in config["inbounds"]], ["http_IN", "socks_IN"])


if __name__ == '__main__':
    unittest.main()
