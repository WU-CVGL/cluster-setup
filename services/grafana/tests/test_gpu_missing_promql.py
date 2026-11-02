"""Exercise the provisioned GPU-missing expression with Prometheus' evaluator."""

import pathlib
import shutil
import subprocess
import tempfile
import unittest

import yaml


class GpuMissingPromQL(unittest.TestCase):
    def test_expected_card_counts_and_exporter_failure(self):
        promtool = shutil.which("promtool")
        command = [promtool] if promtool else None
        image = "prom/prometheus:v3.11.3"
        if command is None:
            if not shutil.which("docker"):
                self.skipTest(
                    "promtool or Docker with the Prometheus image is required"
                )
            available = subprocess.run(
                ["docker", "image", "inspect", image], capture_output=True
            )
            if available.returncode:
                self.skipTest("the production Prometheus image is not cached")
        config = (
            pathlib.Path(__file__).parents[1] / "provisioning/alerting/gpu-health.yaml"
        )
        rules = yaml.safe_load(config.read_text())["groups"][0]["rules"]
        rule = next(r for r in rules if r["uid"] == "gpu-missing")
        evaluator = rule["data"][2]["model"]["conditions"][0]["evaluator"]
        self.assertEqual(evaluator, {"type": "gt", "params": [0]})
        cases = []
        for node, cards, up, deficit in [
            ("s8030", 5, 1, 0),
            ("s8030", 4, 1, 1),
            ("s8030", 0, 1, 5),
            ("cvgl-node01.lan", 8, 1, 0),
            ("cvgl-node01.lan", 7, 1, 1),
            ("cvgl-node01.lan", 0, 1, 8),
            ("s8030", 0, 0, 0),
        ]:
            inputs = [{"series": f'up{{job="dcgm",node="{node}"}}', "values": str(up)}]
            inputs += [
                {
                    "series": f'DCGM_FI_DEV_GPU_TEMP{{job="dcgm",node="{node}",gpu_uuid="gpu{i}"}}',
                    "values": "60",
                }
                for i in range(cards)
            ]
            samples = (
                [{"labels": f'{{__name__="missing",node="{node}"}}', "value": deficit}]
                if deficit
                else []
            )
            cases.append(
                {
                    "input_series": inputs,
                    "promql_expr_test": [
                        {
                            "expr": "missing > 0",
                            "eval_time": "0m",
                            "exp_samples": samples,
                        }
                    ],
                }
            )
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            root.chmod(0o755)
            (root / "rules.yml").write_text(
                yaml.safe_dump(
                    {
                        "groups": [
                            {
                                "name": "test",
                                "rules": [
                                    {
                                        "record": "missing",
                                        "expr": rule["data"][0]["model"]["expr"],
                                    }
                                ],
                            }
                        ]
                    }
                )
            )
            (root / "tests.yml").write_text(
                yaml.safe_dump({"rule_files": ["rules.yml"], "tests": cases})
            )
            for fixture in root.iterdir():
                fixture.chmod(0o644)
            if command is None:
                command = [
                    "docker",
                    "run",
                    "--rm",
                    "--entrypoint",
                    "promtool",
                    "-v",
                    f"{root}:/test:ro",
                    "-w",
                    "/test",
                    image,
                ]
            result = subprocess.run(
                command + ["test", "rules", "tests.yml"],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
