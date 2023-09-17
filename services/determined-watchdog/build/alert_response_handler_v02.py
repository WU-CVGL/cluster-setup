#  TODO restruce the code use the calss feature
#  complete the  figure and the document
#  emo ..  lol
#  add automatic update api and restart the promethus severce (done)
#  class APIHandler  MessageNotifier  MainApplication config (done)

import os
import json
import requests
import subprocess
from flask import Flask, request
import glob
import threading

from datetime import datetime
import time

from alert_config import Config
from alert_MessageNotifier import MessageNotifier
from alert_APIHandler import APIHandler
from alert_DataProcessor import DataProcessor


# TODO
# 配置文件一体化 函数用类包装 然后功能区分隔开


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

    def auto_update(self):
        # access the det api
        curl_command = [
            "curl",
            "-s",
            "http://10.0.1.66:8080/api/v1/auth/login",
            "-H",
            "Content-Type: application/json",
            "--data-binary",
            '{"username":"admin","password":""}',
        ]
        # 使用subprocess运行curl命令并捕获输出
        try:
            completed_process = subprocess.run(
                curl_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            curl_output = completed_process.stdout.strip()
            # 解析JSON输出以获取token
            response_data = json.loads(curl_output)
            token = response_data.get("token", "")
            print(f"Obtained token: {token}")
            # 将token存储在DET_API_TOKEN变量中
            self.config.det_api_token = token

            self.config.det_headers = {
                "Authorization": "Bearer " + self.config.det_api_token
            }
            print(f"self.config.det_headers: {self.config.det_headers}")
            # modify the config and restart the service
            # 源文件路径
            # vim ~/lins_services/services/supplementary/prometheus/config/prometheus.yml
            file_path = "/home/linsadmin/lins_services/services/supplementary/prometheus/config/prometheus.yml"
            # 打开文件以读取内容
            with open(file_path, "r") as file:
                lines = file.readlines()

            # 检查文件是否为空
            if lines:
                # 修改最后一行的内容
                # do not change !
                lines[-1] = f'    bearer_token: "{self.config.det_api_token}"\n'
                # 将修改后的内容写回文件
                with open(file_path, "w") as file:
                    file.writelines(lines)
                print("yml has updated! ")
                # docker compose up -d --force-recreate --remove-orphans prometheus

            else:
                print("path error  fail to modify the yml")
                return
            # docker compose up -d --force-recreate --remove-orphans prometheus
            curl_command = [
                "docker",
                "compose",
                "up",
                "-d",
                "--force-recreate",
                "--remove-orphans",
                "prometheus",
            ]
            result = subprocess.run(
                curl_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )

            # 打印标准输出和标准错误
            print("\n stdout:")
            print(result.stdout)

            print("\n stderr:")
            print(result.stderr)

            # 打印命令的返回代码
            print("\n returncode:", result.returncode)

            # notice  amdin the completion of update the det

            self.message_notifier.send_slack_warning(
                "notification",
                "Automatic update success ~",
                True,
                self.config.debug_slack_webhook_url,
                self.config.run_slack_webhook_url,
            )

        except subprocess.CalledProcessError as e:
            print(f"Error running curl command: {e}")
            self.message_notifier.send_slack_warning(
                "ERROR",
                "Automatic update FAILED ~",
                True,
                self.config.debug_slack_webhook_url,
                self.config.run_slack_webhook_url,
            )

        self.self_check()

    def self_check(self):
        print(f" self.config.det_headers: { self.config.det_headers}")
        print(self.config.grafana_headers)

    def handle_alert_data_v3(self, alert_container_ids):
        if not alert_container_ids:
            print(f"no alert_container_ids.")
            return

        if self.config.alert_name not in alert_container_ids:
            print(f"Key '{self.config.alert_name}' not found in alert_data.")
            self.message_notifier.send_slack_warning(
                "id not found in det", alert_container_ids, True
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
            self.config.last_output_path = "{}/{}".format(
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
                container_ids_to_kill, config.is_debug, config.det_headers
            )
            self.message_notifier.send_slack_notification(
                new_alerts,
                container_ids_to_kill,
                user_data,
                config.is_debug,
                config.debug_slack_webhook_url,
                config.run_slack_webhook_url,
            )
            info = self.DataProcessor.save_json_file(
                new_alerts,
                config.base_path,
                "localData",
                config.alert_name,
                config.alert_name,
            )
            self.DataProcessor.set_file_info(
                config.alert_name, info, "alert_local_item", config.file_info_path
            )

        else:
            self.message_notifier.send_slack_notification(
                new_alerts,
                container_ids_to_kill,
                user_data,
                config.is_debug,
                config.debug_slack_webhook_url,
                config.run_slack_webhook_url,
            )
            info = self.DataProcessor.save_json_file(
                new_alerts,
                config.base_path,
                "localData",
                config.alert_name,
                config.alert_name,
            )
            self.DataProcessor.set_file_info(
                config.alert_name, info, "alert_local_item", config.file_info_path
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
    config = Config(is_debug=False)
    app = MainApplication(config)
    app.run()
