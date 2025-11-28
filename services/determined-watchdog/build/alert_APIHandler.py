import json
import requests
from urllib.parse import urljoin

from alert_config import Config
from alert_MessageNotifier import MessageNotifier


class APIHandler:
    def __init__(self, config: Config):
        self.config = config
        self.message_notifier = MessageNotifier(config)
        self.det_shell_api = urljoin(config.det_web, "api/v1/shells/")
        self.det_task_api = urljoin(config.det_web, "api/v1/tasks/")
        self.grafana_alart_api = urljoin(
            config.grafana_web,
            "api/alertmanager/grafana/api/v2/alerts/"
        )
        self.initialize_file_info()

    def initialize_file_info(self):
        # Construct JSON data
        data = {
            "file_group_name": self.config.file_info_name,
            "alert_item": [],
            "alert_local_item": [],
        }

        for file_name in self.config.alert_types:
            file_item = {}
            for item in self.config.sub_item:
                file_item[item] = ""
                if item == "alert_type":
                    file_item[item] = file_name
            data["alert_item"].append(file_item)

        # Info about after filtering calculate it record?
        for record in self.config.alert_types:
            record_item = {}
            for item in self.config.sub_item:
                record_item[item] = ""
                if item == "alert_type":
                    record_item[item] = record
            data["alert_local_item"].append(record_item)

        # Write the data to a JSON file
        with open(self.config.file_info_path, "w") as f:
            json.dump(data, f, indent=4)

    def get_shell_api_data(self):
        shell_api_response = requests.get(
            url=self.det_shell_api,
            headers=self.config.det_headers,
            verify=False,
        )  # ignore SSL verification
        return shell_api_response.json()

    def get_task_api_data(self):
        task_api_response = requests.get(
            url=self.det_task_api,
            headers=self.config.det_headers,
            verify=False,
        )
        return task_api_response.json()

    def parse_api_data(self, shell_api_data, task_api_data):
        result = {}
        if "error" in shell_api_data:
            self.message_notifier.send_slack_warning(
                "det api miss",
                "need update api!",
                self.config.slack_webhook_url,
            )
            return {}
        if "error" in task_api_data:
            self.message_notifier.send_slack_warning(
                "det api miss",
                "need update api!",
                self.config.slack_webhook_url,
            )
            return {}
        for shell in shell_api_data["shells"]:
            shell_id = shell["id"]

            ## container is null in shell_api. Retrieve from task_api
            # container_id = shell["container"].get("id")
            try:
                shell_task_data = task_api_data["allocationIdToSummary"][f"{shell_id}.1"]
            except KeyError:
                continue
            resources = shell_task_data["resources"]
            if resources is not None and len(resources) > 0:
                container_id = resources[0]["containerId"]
                # print(f"[debug] container_id: {container_id} for shell_id {shell_id}")
                if container_id is None:
                    print("[debug] container_id is none")
                    print(task_api_data)
                    print(shell_id)

                description = shell.get("description")
                username = shell.get("username")
                startTime = shell.get("startTime")
                devices = resources[0]["agentDevices"].get("devices", [])
                device_count = len(devices)
                if shell_id is not None and shell_id not in result:
                    result[shell_id] = {
                        "container_id": container_id,
                        "description": description,
                        "username": username,
                        "startTime": startTime,
                        "device_count": device_count,
                        "devices": devices,
                    }
        return result

    def get_alert_rules(self):
        endpoint = self.grafana_alart_api
        try:
            # 发送 GET 请求获取警报规则列表
            response = requests.get(
                endpoint, headers=self.config.grafana_headers, verify=False
            )
            # 检查响应状态码并处理结果
            if response.status_code == 200:
                alert_rules = response.json()
                return alert_rules
            else:
                print(f"Failed to get alert rules. Error: {response.text}")
        except requests.exceptions.RequestException as e:
            print(f"Failed to connect to Grafana: {e}")

        return None

    def get_container_ids_by_alertname(self, alert_data):
        container_ids = {}
        for alert in alert_data:
            if "labels" in alert and "alertname" in alert["labels"]:
                alertname = alert["labels"]["alertname"]
                container_id = alert["labels"].get("container_id")
                if alertname not in container_ids:
                    container_ids[alertname] = set()
                if container_id:
                    container_ids[alertname].add(container_id)
        return container_ids

    def kill_container(self, shell_id, det_header, debug):
        if debug is True:
            try:
                response = requests.get(
                    url=urljoin(self.det_shell_api, shell_id),
                    headers=det_header,
                    verify=False,
                )
                response.raise_for_status()
                print(response)
            except requests.exceptions.RequestException as e:
                print(f"Failed to retrieve shell {shell_id}. Error: {e}")
        else:
            try:
                response = requests.post(
                    url=urljoin(self.det_shell_api, f"{shell_id}/kill"),
                    headers=det_header,
                    verify=False,
                )
                response.raise_for_status()
            except requests.exceptions.RequestException as e:
                print(f"Failed to kill shell {shell_id}. Error: {e}")

    def kill_containers(self, alert_container_ids, debug, det_headers):
        for shell_id in alert_container_ids:
            self.kill_container(shell_id, det_headers, debug)
            print("\n")
