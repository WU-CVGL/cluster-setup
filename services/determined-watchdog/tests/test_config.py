import os
import subprocess
import sys
import unittest
from pathlib import Path

import watchdog_test_support as support

import alert_config
from alert_config import Config, ConfigError, parse_bool

ENV = {
    "WATCHDOG_DEBUG": "0",
    "DET_WEB_URL": "http://det.invalid:8080",
    "DET_USERNAME": "admin",
    "DET_PASSWORD": support.FAKE_PASSWORD,
    "GRAFANA_WEB_URL": "http://grafana.invalid:3000",
    "GRAFANA_API_TOKEN": support.FAKE_GRAFANA_TOKEN,
    "GRAFANA_ALERT_NAME": "IdleKillAlert",
    "SLACK_WEBHOOK_URL": "https://hooks.slack.invalid/services/PROD-PLACEHOLDER",
    "SLACK_WEBHOOK_URL_DEBUG": "https://hooks.slack.invalid/services/DEBUG-PLACEHOLDER",
}


class DebugFlagTest(unittest.TestCase):
    def test_true_values(self):
        for value in ["1", "true", "TRUE", "True", "yes", "YES", "on", "On", "ON", " true \n"]:
            with self.subTest(value=value):
                self.assertTrue(parse_bool(value))

    def test_false_values(self):
        for value in [None, "", "0", "false", "False", "no", "off", "2", "enable", "tru"]:
            with self.subTest(value=value):
                self.assertFalse(parse_bool(value))

    def test_from_env_uses_parsed_flag(self):
        for value, expected in [("true", True), ("ON", True), ("0", False), ("off", False)]:
            with self.subTest(value=value):
                config = Config.from_env(dict(ENV, WATCHDOG_DEBUG=value))
                self.assertIs(config.is_debug, expected)


class FromEnvTest(unittest.TestCase):
    def test_production(self):
        config = Config.from_env(ENV)
        self.assertEqual(config.base_path, Path("/app/data"))
        self.assertEqual(config.file_info_path, Path("/app/data/file_info.json"))
        self.assertEqual(config.slack_webhook_url, ENV["SLACK_WEBHOOK_URL"])
        self.assertEqual(config.det_metrics_token_path, Path("/run/determined-metrics/token"))
        self.assertEqual(config.alert_name, "IdleKillAlert")
        self.assertEqual(config.alert_min, 0)
        self.assertEqual(config.warning_max_age_minutes, 90)
        self.assertEqual(config.warning_min_age_minutes, 30)
        self.assertEqual(config.token_renew_hours, 48)
        self.assertEqual(
            config.grafana_headers, {"Authorization": "Bearer " + support.FAKE_GRAFANA_TOKEN}
        )

    def test_debug(self):
        config = Config.from_env(dict(ENV, WATCHDOG_DEBUG="1"))
        self.assertEqual(config.base_path, Path("/app/data/debug"))
        self.assertEqual(config.slack_webhook_url, ENV["SLACK_WEBHOOK_URL_DEBUG"])

    def test_optional_overrides(self):
        config = Config.from_env(
            dict(
                ENV,
                DATA_DIR="/tmp/d",
                DATA_DIR_DEBUG="/tmp/dd",
                DETERMINED_METRICS_TOKEN_FILE="/run/tok",
            )
        )
        self.assertEqual(config.base_path, Path("/tmp/d"))
        self.assertEqual(config.det_metrics_token_path, Path("/run/tok"))
        config = Config.from_env(dict(ENV, WATCHDOG_DEBUG="yes", DATA_DIR_DEBUG="/tmp/dd"))
        self.assertEqual(config.base_path, Path("/tmp/dd"))

    def test_missing_required(self):
        env = dict(ENV)
        del env["DET_PASSWORD"]
        del env["GRAFANA_ALERT_NAME"]
        with self.assertRaises(ConfigError) as ctx:
            Config.from_env(env)
        self.assertIn("DET_PASSWORD", str(ctx.exception))
        self.assertIn("GRAFANA_ALERT_NAME", str(ctx.exception))

    def test_portainer_and_prometheus_vars_not_required(self):
        self.assertNotIn("PORTAINER_WEB_URL", alert_config.REQUIRED_ENV)
        config = Config.from_env(ENV)  # none of them set
        self.assertIsInstance(config, Config)
        env = dict(ENV, PORTAINER_API_TOKEN="PLACEHOLDER", PROMETHEUS_WEB_URL="http://x")
        self.assertEqual(
            alert_config.obsolete_env_names(env), ["PORTAINER_API_TOKEN", "PROMETHEUS_WEB_URL"]
        )


class RedactionTest(unittest.TestCase):
    def test_str_and_repr_redact_secrets(self):
        config = Config.from_env(ENV)
        for text in (str(config), repr(config), "%s" % config, f"{config!r}"):
            with self.subTest(text=text[:30]):
                self.assertNotIn(support.FAKE_PASSWORD, text)
                self.assertNotIn(support.FAKE_GRAFANA_TOKEN, text)
                self.assertNotIn("PROD-PLACEHOLDER", text)
                self.assertIn("det_password: <redacted>", text)
                self.assertIn("grafana_api_token: <redacted>", text)
                self.assertIn("slack_webhook_url: <redacted>", text)
                # non-secret settings stay visible
                self.assertIn("det_username: admin", text)
                self.assertIn("det_metrics_token_path: /run/determined-metrics/token", text)

    def test_redact_helper(self):
        text = alert_config.redact("a SECRET and SECRET2 b", ["SECRET", "SECRET2", None, ""])
        self.assertEqual(text, "a <redacted> and <redacted> b")


class ImportWithoutEnvTest(unittest.TestCase):
    def test_modules_import_with_empty_environment(self):
        code = (
            "import sys; sys.dont_write_bytecode = True; sys.path.insert(0, %r); "
            "import alert_config, alert_MessageNotifier, alert_DataProcessor, "
            "alert_APIHandler, alert_TokenManager, alert_response_handler_v02"
            % str(support.BUILD_DIR)
        )
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_main_reports_missing_env(self):
        code = (
            "import sys; sys.dont_write_bytecode = True; sys.path.insert(0, %r); "
            "import alert_response_handler_v02 as m; sys.exit(m.main())" % str(support.BUILD_DIR)
        )
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("missing required environment variable(s): WATCHDOG_DEBUG", result.stderr)


if __name__ == "__main__":
    unittest.main()
