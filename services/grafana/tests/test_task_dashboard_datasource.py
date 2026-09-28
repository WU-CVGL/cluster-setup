"""Guard the Grafana task dashboard's datasource binding contract."""

import json
import unittest
from pathlib import Path


DASHBOARD = (
    Path(__file__).resolve().parents[1]
    / "provisioning/dashboards/json/determined-task-resources.json"
)
PROMETHEUS_REF = {"type": "prometheus", "uid": "${prometheus_source}"}


class TaskDashboardDatasourceTest(unittest.TestCase):
    def test_all_queries_use_the_selected_prometheus_uid(self):
        dashboard = json.loads(DASHBOARD.read_text())
        variables = dashboard["templating"]["list"]
        source, *selectors = variables

        self.assertEqual(source["name"], "prometheus_source")
        self.assertEqual(source["type"], "datasource")
        self.assertEqual(source["query"], "prometheus")
        self.assertEqual(source["hide"], 0)
        self.assertEqual(
            [variable["name"] for variable in selectors],
            ["cluster", "task_id", "allocation_id"],
        )
        for variable in selectors:
            self.assertEqual(variable["query"]["qryType"], 3)
            self.assertEqual(
                variable["query"]["refId"],
                "PrometheusVariableQueryEditor-VariableQuery",
            )
            self.assertEqual(variable["query"]["query"], variable["definition"])
        self.assertFalse(selectors[0]["multi"] or selectors[0]["includeAll"])
        self.assertFalse(selectors[1]["multi"] or selectors[1]["includeAll"])
        self.assertTrue(selectors[2]["includeAll"])
        self.assertEqual(selectors[2]["allValue"], ".*")

        references = []

        def visit(value):
            if isinstance(value, dict):
                if "datasource" in value:
                    references.append(value["datasource"])
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(dashboard)
        self.assertGreater(len(references), len(selectors))
        self.assertTrue(all(ref == PROMETHEUS_REF for ref in references))
        self.assertTrue(all(variable["datasource"] == PROMETHEUS_REF for variable in selectors))
        serialized = DASHBOARD.read_text()
        self.assertNotIn(":regex}", serialized)
        self.assertNotIn('task_id=~', serialized)
        self.assertNotIn('det_cluster=~', serialized)


if __name__ == "__main__":
    unittest.main()
