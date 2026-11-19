"""
The probe success queries evaluated by Prometheus itself, with promtool from PATH or from the Prometheus image
docker-compose.yml pins (only when Docker has it already, and with no network); without either, that test is skipped.
services/prometheus/tests/proxy-status-bars.test.yml runs the status bars' queries (a label change inside a bucket,
two series at once, partly probed and empty buckets), proxy-24h-success.test.yml the proxies' 24-hour success
rates and coverage (a label change inside the 24 hours, two series at once, a partly probed protocol), and
proxy-live-status.test.yml the proxies' live status and the HA entries' proxy counts (reclassified proxies whose old
labels were up within the day, a probe without results, a removed probe, unchanged proxies).
"""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from portal import queries

SERVICES = Path(__file__).resolve().parents[3]
TESTS = SERVICES / "prometheus" / "tests"
PROXIES = queries.STATUS["proxies"]
HA = [queries.STATUS[entry] for entry in ("ha-general", "ha-private")]
FIXTURES = {
    "proxy-status-bars.test.yml": {expr for exprs in queries.BARS.values() for expr in exprs.values()},
    "proxy-24h-success.test.yml": {PROXIES["rate_24h_http"], PROXIES["rate_24h_socks"], PROXIES["coverage_24h"]},
    "proxy-live-status.test.yml": {PROXIES["http"], PROXIES["socks"]} |
                                  {ha[key] for ha in HA for key in ("proxies_passing", "proxies_configured")},
}


def fixture_exprs(name):
    return set(re.findall(r"^\s*- expr: '(.*)'$", (TESTS / name).read_text(), re.MULTILINE))


def prometheus_image():
    found = re.search(r"^\s*image:\s*(prom/prometheus:\S+)\s*$", (SERVICES / "docker-compose.yml").read_text(),
                      re.MULTILINE)
    return found.group(1) if found else None


class PromQLTest(SimpleTestCase):
    def test_fixtures_test_the_portals_queries(self):
        for name, exprs in FIXTURES.items():
            with self.subTest(fixture=name):
                self.assertEqual(fixture_exprs(name), exprs)

    def test_promtool(self):
        promtool = shutil.which("promtool")
        image = prometheus_image()
        if promtool is None:
            if image is None or shutil.which("docker") is None:
                self.skipTest("promtool or Docker with the Prometheus image of docker-compose.yml is required")
            if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode:
                self.skipTest(f"promtool is not installed and Docker does not have {image}")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o755)
            for name in FIXTURES:
                (root / name).write_text((TESTS / name).read_text())
                (root / name).chmod(0o644)
            command = [promtool] if promtool else [
                "docker", "run", "--rm", "--network", "none", "--entrypoint", "promtool",
                "-v", f"{root}:/test:ro", "-w", "/test", image]
            result = subprocess.run(command + ["test", "rules", *FIXTURES], cwd=root, capture_output=True,
                                    text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((result.stdout + result.stderr).count("SUCCESS"), len(FIXTURES), result.stdout + result.stderr)
