import os
import json
import requests
import subprocess
from flask import Flask, request
from urllib.parse import urljoin
import glob
import threading

from datetime import datetime
import time

from typing import Type

from alert_config import Config
from alert_MessageNotifier import MessageNotifier
from alert_APIHandler import APIHandler
from alert_DataProcessor import DataProcessor


color_green = "\033[36m"
color_yellow = "\033[33m"
color_red = "\033[31m"
color_reset = "\033[0m"


class MainApplication:
    def __init__(self, config: Config):
        self.config = config
        self.api_handler = APIHandler(config)
        self.message_notifier = MessageNotifier(config)
        self.DataProcessor = DataProcessor(config)
        print(config)

    def run(self):
        requests.packages.urllib3.disable_warnings()
        self.auto_update()

        current_time = datetime.now()
        is_next_color_a = True
        next_color = "\033[34m"

        while True:
            now_time = datetime.now()
            # 定时消息通知
            if (
                now_time.minute == self.config.alert_min
                and self.config.time_function_enabled_3090
            ):
                self.config.time_function_enabled_3090 = False
                grafana_alert = self.api_handler.get_alert_rules()
                alert_total = self.api_handler.get_container_ids_by_alertname(
                    grafana_alert
                )
                self.handle_alert_data_v3(alert_total)
            elif (
                now_time.minute == self.config.alert_min
                and not self.config.time_function_enabled_3090
            ):
                pass
            elif now_time.minute != self.config.alert_min:
                self.config.time_function_enabled_3090 = True
            else:
                pass

            # 定时自更新

            if (
                now_time.weekday() == self.config.alert_update_day
                and self.config.time_function_enabled_update
            ):
                self.config.time_function_enabled_update = False
                self.auto_update()
            elif (
                now_time.weekday() == self.config.alert_update_day
                and not self.config.time_function_enabled_update
            ):
                pass
            elif now_time.weekday() != self.config.alert_update_day:
                self.config.time_function_enabled_update = True
            else:
                pass

            if self.config.is_debug:
                grafana_alert = self.api_handler.get_alert_rules()
                alert_total = self.api_handler.get_container_ids_by_alertname(
                    grafana_alert
                )
                self.handle_alert_data_v3(alert_total)
                time.sleep(10)

            next_color, is_next_color_a = get_next_color(
                is_next_color_a, color_green, color_yellow
            )

            print(
                f"start: {current_time}, now: {next_color}{now_time}{color_reset}",
                end="\r",
                flush=True,
            )
            time.sleep(10)

    def renew_det_token(self):
        det_login_api = urljoin(self.config.det_web, "api/v1/auth/login/")
        payload = json.dumps(
            {
                "username": self.config.det_username,
                "password": self.config.det_password,
            }
        )
        headers = {"Content-Type": "application/json"}
        try:
            response = requests.post(det_login_api, headers=headers, data=payload)
        except Exception as e:
            print(e)
            print("Failed to connect to Determined master")
            return

        token = json.loads(response.text).get("token", "")
        print(f"Obtained new Determined AI token.")
        self.config.det_api_token = token
        self.config.det_headers = {
            "Authorization": f"Bearer {self.config.det_api_token}"
        }

    def restart_prometheus(self):
        # https://prometheus.io/docs/prometheus/latest/migration/#prometheus-lifecycle
        prom_reload_api = urljoin(self.config.prom_web, "-/reload/")
        try:
            response = requests.post(prom_reload_api)
        except Exception as e:
            print(e)
            print("Failed to reload Prometheus.")
            return
        print("Prometheus is restarted!")

    def update_det_token_to_prometheus(self):
        with open(self.config.prom_cfg_path, "r") as f:
            lines = f.readlines()
        if lines:
            lines[-1] = f'    bearer_token: "{self.config.det_api_token}"\n'
            with open(self.config.prom_cfg_path, "w") as f:
                f.writelines(lines)
                print("Prometheus config has updated!")
                self.restart_prometheus()
        else:
            print("Fail to update Prometheus config.")
            return

    def auto_update(self):
        try:
            self.renew_det_token()
            self.update_det_token_to_prometheus()

            self.message_notifier.send_slack_warning(
                warning_type="notification",
                info="Automatic update success ~",
                slack_webhook_url=self.config.slack_webhook_url,
            )

        except subprocess.CalledProcessError as e:
            print(f"Error running curl command: {e}")
            self.message_notifier.send_slack_warning(
                warning_type="ERROR",
                info="Automatic update FAILED ~",
                slack_webhook_url=self.config.slack_webhook_url,
            )

        self.self_check()

    def self_check(self):
        print(f"det_headers: { self.config.det_headers}")
        print(f"grafana_headers: {self.config.grafana_headers}")

    def handle_alert_data_v3(self, alert_container_ids):
        if not alert_container_ids:
            print(f"no alert_container_ids.")
            return

        if self.config.alert_name not in alert_container_ids:
            print(f"Key '{self.config.alert_name}' not found in alert_data.")
            self.message_notifier.send_slack_warning(
                warning_type="id not found in det",
                info=alert_container_ids,
                slack_webhook_url=self.config.slack_webhook_url,
            )
            # Slack send blank message?
            return

        api_data = self.api_handler.get_api_data()
        det_container_ids = self.api_handler.parse_api_data(api_data)
        alert_3090_container_ids = alert_container_ids[self.config.alert_name]

        # 解析API数据
        new_data = self.DataProcessor.filter_container_by_id(
            alert_3090_container_ids, det_container_ids
        )
        if not new_data:
            print(f"'{alert_container_ids}' not found in det_container_ids.")
            # Slack send blank message?
            return

        user_file_path = f"{self.config.base_path}/User.json"
        user_data = self.DataProcessor.read_user_info(user_file_path)

        # 获取上次保存的last_output
        self.config.last_output = self.DataProcessor.get_alert_local(
            self.config.alert_name, self.config.file_info_path
        )
        if self.config.last_output is not None:
            self.config.last_output_path = os.path.join(
                self.config.last_output["directory"],
                self.config.last_output["file_name"],
            )
            if os.path.isfile(self.config.last_output_path):
                with open(self.config.last_output_path) as f:
                    old_data = json.load(f)
            else:
                old_data = {}
        else:
            self.config.last_output_path = None
            old_data = {}

        new_alerts = self.DataProcessor.find_new_alerts(new_data, old_data)
        container_ids_to_kill = self.DataProcessor.find_common_alerts(
            new_data, old_data
        )

        if container_ids_to_kill is not None and old_data is not None:
            # 比较警报信息并获取需要传递给kill_containers的容器ID
            self.api_handler.kill_containers(
                container_ids_to_kill,
                self.config.is_debug,
                self.config.det_headers,
            )
            self.message_notifier.send_slack_notification(
                new_alerts,
                container_ids_to_kill,
                user_data,
                self.config.slack_webhook_url,
            )
            info = self.DataProcessor.save_json_file(
                new_alerts,
                self.config.base_path,
                "localData",
                self.config.alert_name,
                self.config.alert_name,
            )
            self.DataProcessor.set_file_info(
                self.config.alert_name,
                info,
                "alert_local_item",
                self.config.file_info_path,
            )

        else:
            self.message_notifier.send_slack_notification(
                new_alerts,
                container_ids_to_kill,
                user_data,
                self.config.slack_webhook_url,
            )
            info = self.DataProcessor.save_json_file(
                new_alerts,
                self.config.base_path,
                "localData",
                self.config.alert_name,
                self.config.alert_name,
            )
            self.DataProcessor.set_file_info(
                self.config.alert_name,
                info,
                "alert_local_item",
                self.config.file_info_path,
            )


def get_next_color(is_color_a: bool, color_a: str, color_b: str) -> tuple:
    if is_color_a:
        next_color = color_b
        is_next_color_a = False
    else:
        next_color = color_a
        is_next_color_a = True

    return next_color, is_next_color_a


if __name__ == "__main__":
    config = Config()
    app = MainApplication(config)
    app.run()
