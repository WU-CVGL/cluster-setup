import json
import os
from urllib.parse import urljoin

import requests

from alert_config import (
    HTTP_TIMEOUT,
    TASK_KIND_DEFAULT,
    TASK_KIND_NOTEBOOK,
    TASK_KIND_SHELL,
    Config,
    log,
)
from alert_DataProcessor import write_json_atomic


class DetAPIError(Exception):
    """A Determined API call failed or returned something unusable.

    status_code is the HTTP status of a non-2xx response, None for any other failure.
    """

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class APIHandler:
    def __init__(self, config: Config):
        self.config = config
        self.det_shell_api = urljoin(config.det_web, "api/v1/shells/")
        self.det_notebook_api = urljoin(config.det_web, "api/v1/notebooks/")
        self.det_task_api = urljoin(config.det_web, "api/v1/tasks/")
        # Base URL of each policed kind: <base><id>/kill kills it, <base><id> gets it.
        self.det_kind_api = {
            TASK_KIND_SHELL: self.det_shell_api,
            TASK_KIND_NOTEBOOK: self.det_notebook_api,
        }
        self.grafana_alert_api = urljoin(
            config.grafana_web,
            "api/alertmanager/grafana/api/v2/alerts/"
        )
        self.initialize_file_info()

    def _file_info_is_valid(self):
        try:
            with open(self.config.file_info_path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict) or not isinstance(data.get("alert_local_item"), list):
            return False
        for item in data["alert_local_item"]:
            if isinstance(item, dict) and item.get("alert_type") == self.config.alert_name:
                return all(isinstance(item.get(key), str) for key in self.config.sub_item)
        return False

    def initialize_file_info(self):
        """Create file_info.json unless a valid one exists (the warning state survives restarts)."""
        os.makedirs(self.config.base_path, exist_ok=True)
        if self._file_info_is_valid():
            log(f"Keeping existing {self.config.file_info_path}.")
            return
        if os.path.exists(self.config.file_info_path):
            log(f"{self.config.file_info_path} is invalid, re-initializing it.")

        # Construct JSON data
        data = {
            "file_group_name": self.config.file_info_name,
            "alert_item": [],
            "alert_local_item": [],
        }
        for group in ("alert_item", "alert_local_item"):
            record_item = {}
            for item in self.config.sub_item:
                record_item[item] = ""
                if item == "alert_type":
                    record_item[item] = self.config.alert_name
            data[group].append(record_item)

        write_json_atomic(self.config.file_info_path, data, indent=4)

    def _get_det_json(self, url, det_headers, what):
        try:
            response = requests.get(
                url=url,
                headers=det_headers,
                verify=False,  # ignore SSL verification
                timeout=HTTP_TIMEOUT,
            )
        except requests.exceptions.RequestException as e:
            raise DetAPIError(f"{what}: {type(e).__name__}: {e}") from e
        if not 200 <= response.status_code < 300:
            raise DetAPIError(f"{what}: HTTP {response.status_code}", status_code=response.status_code)
        try:
            data = response.json()
        except ValueError:
            raise DetAPIError(f"{what}: non-JSON response") from None
        if not isinstance(data, dict) or "error" in data:
            raise DetAPIError(f"{what}: unexpected response {str(data)[:200]}")
        return data

    def get_shell_api_data(self, det_headers):
        data = self._get_det_json(self.det_shell_api, det_headers, "GET shells")
        if not isinstance(data.get("shells") or [], list):
            raise DetAPIError("GET shells: 'shells' is not a list")
        return data

    def get_notebook_api_data(self, det_headers):
        data = self._get_det_json(self.det_notebook_api, det_headers, "GET notebooks")
        if not isinstance(data.get("notebooks") or [], list):
            raise DetAPIError("GET notebooks: 'notebooks' is not a list")
        return data

    def get_task_api_data(self, det_headers):
        data = self._get_det_json(self.det_task_api, det_headers, "GET tasks")
        if not isinstance(data.get("allocationIdToSummary") or {}, dict):
            raise DetAPIError("GET tasks: 'allocationIdToSummary' is not a map")
        return data

    def parse_api_data(self, shell_api_data, task_api_data, notebook_api_data=None):
        """Return {task_id: info} for the shells and notebooks that have a container.

        info["kind"] is "shell" or "notebook". shell_api_data or notebook_api_data may be None
        (that listing failed). A notebook's serviceAddress carries its Jupyter token: it is not
        copied (info is logged and saved).
        """
        result = {}
        summaries = task_api_data.get("allocationIdToSummary") or {}
        for kind, tasks in (
            (TASK_KIND_SHELL, (shell_api_data or {}).get("shells")),
            (TASK_KIND_NOTEBOOK, (notebook_api_data or {}).get("notebooks")),
        ):
            for task in tasks or []:
                self._add_task_container(result, kind, task, summaries)
        return result

    def _add_task_container(self, result, kind, task, summaries):
        task_id = task.get("id")
        if task_id is None:
            return

        ## container is null in shell_api / notebook_api. Retrieve from task_api:
        ## shells and notebooks have one allocation, "<task id>.1"
        task_data = summaries.get(f"{task_id}.1")
        if not task_data:
            return
        resources = task_data.get("resources")
        if not resources:
            return
        container_id = resources[0].get("containerId")
        if container_id is None:
            print(f"[debug] container_id is none for {kind} {task_id}")
            return

        # agentDevices: {agent_id: {"devices": [...]}}, may be null
        devices = []
        for agent in (resources[0].get("agentDevices") or {}).values():
            if isinstance(agent, dict):
                devices.extend(agent.get("devices") or [])

        if task_id not in result:
            result[task_id] = {
                "kind": kind,
                "container_id": container_id,
                "description": task.get("description"),
                "username": task.get("username"),
                "startTime": task.get("startTime"),
                "device_count": len(devices),
                "devices": devices,
            }

    def get_alert_rules(self):
        """Firing alerts from Grafana's Alertmanager; silenced and inhibited alerts are excluded."""
        endpoint = self.grafana_alert_api
        params = {"active": "true", "silenced": "false", "inhibited": "false"}
        try:
            # 发送 GET 请求获取警报列表
            response = requests.get(
                endpoint,
                headers=self.config.grafana_headers,
                params=params,
                verify=False,
                timeout=HTTP_TIMEOUT,
            )
        except requests.exceptions.RequestException as e:
            log(f"Failed to connect to Grafana: {e}")
            return None
        # 检查响应状态码并处理结果
        if response.status_code != 200:
            log(f"Failed to get alert rules. HTTP {response.status_code}: {response.text[:200]}")
            return None
        try:
            alert_rules = response.json()
        except ValueError:
            log("Failed to get alert rules: non-JSON response.")
            return None
        if not isinstance(alert_rules, list):
            log(f"Failed to get alert rules: unexpected response {str(alert_rules)[:200]}")
            return None
        return alert_rules

    def get_container_ids_by_alertname(self, alert_data):
        """Return {alertname: set(container_id)}."""
        container_ids = {}
        for alert in alert_data:
            labels = alert.get("labels") if isinstance(alert, dict) else None
            if isinstance(labels, dict) and "alertname" in labels:
                alertname = labels["alertname"]
                container_id = labels.get("container_id")
                if alertname not in container_ids:
                    container_ids[alertname] = set()
                if container_id:
                    container_ids[alertname].add(container_id)
        return container_ids

    def kill_container(self, task_id, det_header, debug, kind=TASK_KIND_DEFAULT) -> bool:
        """Kill one shell or notebook; in debug mode only GET it (dry run). True on success."""
        base_api = self.det_kind_api.get(kind)
        if base_api is None:
            log(f"Cannot kill task {task_id}: unknown kind {kind!r}.")
            return False
        try:
            if debug is True:
                response = requests.get(
                    url=urljoin(base_api, task_id),
                    headers=det_header,
                    verify=False,
                    timeout=HTTP_TIMEOUT,
                )
            else:
                response = requests.post(
                    url=urljoin(base_api, f"{task_id}/kill"),
                    headers=det_header,
                    verify=False,
                    timeout=HTTP_TIMEOUT,
                )
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            action = "retrieve (debug)" if debug is True else "kill"
            log(f"Failed to {action} {kind} {task_id}. Error: {e}")
            return False
        if debug is True:
            log(f"[debug] would kill {kind} {task_id}: {response}")
        return True

    def kill_containers(self, tasks, debug, det_headers):
        """Kill the given {task_id: info} shells and notebooks (by info["kind"], default shell).

        Returns (killed, failed), both {task_id: info}.
        """
        killed, failed = {}, {}
        for task_id, info in tasks.items():
            kind = info.get("kind", TASK_KIND_DEFAULT)
            if self.kill_container(task_id, det_headers, debug, kind):
                killed[task_id] = info
            else:
                failed[task_id] = info
        return killed, failed
