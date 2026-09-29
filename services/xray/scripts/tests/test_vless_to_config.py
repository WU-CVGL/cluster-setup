# -*- coding: utf-8 -*-
"""
vless_to_config.py: 默认输出与 fixtures/vless_to_config_default.json 模板一致，
--stats-api 输出与 create_xray_service.py 写入的 config.json 模板一致 (只少了日志文件路径)，
-o 写入的文件与 stdout 输出相同。
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from corpus import (  # noqa: E402
    CREATE_SERVICE_TEMPLATE, SCRIPTS_DIR, VLESS_TO_CONFIG_TEMPLATE, expected_config, load_cases,
)

SCRIPT = SCRIPTS_DIR / "vless_to_config.py"


def run(*args):
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPT)] + list(args),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, encoding="utf-8",
    )


class VlessToConfigTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.cases = [case for case in load_cases() if case['url'].startswith('vless://')]

    def test_default_output(self):
        checked = 0
        for case in self.cases:
            with self.subTest(case['name']):
                result = run(case['url'])
                if 'error' in case:
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(case['error'], result.stderr)
                    continue
                self.assertEqual(result.returncode, 0, result.stderr)
                # 模板中只替换 outbound，其余部分必须逐字节一致
                self.assertEqual(result.stdout, expected_config(VLESS_TO_CONFIG_TEMPLATE, case['outbound']) + "\n")
                checked += 1
        self.assertGreaterEqual(checked, 16)

    def test_stats_api_matches_compose_template(self):
        checked = 0
        for case in self.cases:
            if 'error' in case:
                continue
            with self.subTest(case['name']):
                result = run(case['url'], "--stats-api", "--loglevel", "none")
                self.assertEqual(result.returncode, 0, result.stderr)
                # 与 create_xray_service.py 写入的 config.json 相比，只少了日志文件路径
                expected = expected_config(CREATE_SERVICE_TEMPLATE, case['outbound'], log={"loglevel": "none"})
                self.assertEqual(result.stdout, expected + "\n")
                config = json.loads(result.stdout)
                self.assertEqual(list(config),
                                 ["log", "stats", "api", "dns", "policy", "inbounds", "outbounds", "routing"])
                self.assertIn({"inboundTag": ["api"], "outboundTag": "api", "type": "field", "enabled": True},
                              config["routing"]["rules"])
                self.assertEqual(config["inbounds"][-1]["port"], 10085)
                checked += 1
        self.assertGreaterEqual(checked, 16)

    def test_output_file(self):
        # -o: 自动创建父目录，文件内容与 stdout 输出相同 (末尾有换行，非 ASCII 字符不转义)，stdout 为空
        case = next(case for case in self.cases if case['name'] == 'vless_remark_pct_utf8')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "config.json"
            result = run(case['url'], "-o", str(path))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertIn("Wrote config", result.stderr)
            self.assertEqual(path.read_bytes().decode("utf-8"),
                             expected_config(VLESS_TO_CONFIG_TEMPLATE, case['outbound']) + "\n")


if __name__ == '__main__':
    unittest.main()
