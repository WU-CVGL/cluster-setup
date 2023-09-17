from __future__ import annotations
import os
import json
import requests
import subprocess
from flask import Flask, request
import glob
import threading

from datetime import datetime
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alert_config import Config


class DataProcessor:
    def __init__(self, config: Config):
        self.config = config

    def get_sub_items(self, alert_type, path, group):
        with open(path) as f:
            file_info = json.load(f)
        for item in file_info[group]:
            if item["alert_type"] == alert_type:
                return item
        return None

    def modify_alert_item(self, alert_type, key, value, group, data):
        print(value)
        for item in data[group]:
            if item["alert_type"] == alert_type:
                item[key] = value
                break
        else:
            print("No such alert type")
            return False
        return True

    def set_file_info(self, alert_type, info, group, file_path):
        with open(file_path) as f:
            data = json.load(f)
        self.modify_alert_item(alert_type, "file_name", info["file_name"], group, data)
        self.modify_alert_item(alert_type, "directory", info["directory"], group, data)
        self.modify_alert_item(
            alert_type, "created_at", info["created_at"], group, data
        )
        self.modify_alert_item(alert_type, "file_type", info["file_type"], group, data)

        with open(file_path, "w") as f:
            json.dump(data, f, indent=4)
        return True

    # 通用日志保存函数,返回信息用于记录在file中
    def save_json_file(self, data, base_path, folder_name, alert_type, file_type):
        now = datetime.now()
        directory = os.path.join(
            base_path, folder_name, now.strftime("%Y-%m"), now.strftime("%Y-%m-%d")
        )
        os.makedirs(directory, exist_ok=True)

        file_name = f"{folder_name}_{now.strftime('%Y%m%d%H%M%S')}.json"
        file_path = os.path.join(directory, file_name)
        print(data)
        with open(file_path, "w") as f:
            json.dump(data, f)

        info = {
            "alert_type": alert_type,
            "file_name": file_name,
            "directory": directory,
            "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "file_type": file_type,
        }

        return info

    def extract_container_ids(self, alert_data):
        container_ids = set()
        alerts = alert_data.get("alerts", [])
        for alert in alerts:
            labels = alert.get("labels", {})
            container_id = labels.get("container_id")
            if container_id:
                container_ids.add(container_id)
        return container_ids

    def filter_container_by_id(self, container_ids, container_data):
        filtered_data = {}
        for container_id, data in container_data.items():
            if data["container_id"] in container_ids:
                filtered_data[container_id] = data
        return filtered_data

    def read_user_info(self, user_file_path):
        with open(user_file_path) as f:
            user_data = json.load(f)
        return user_data

    def extract_alert_info(self, alert_data, container_ids):
        result = {}
        alerts = alert_data.get("alerts", [])
        for alert in alerts:
            labels = alert.get("labels", {})
            container_id = labels.get("container_id")
            if container_id in container_ids:
                container_info = container_ids[container_id]
                task_id = container_info.get("container_id")
                username = container_info.get("username")
                description = container_info.get("description")
                if task_id not in result:
                    result[task_id] = {
                        "container_id": container_id,
                        "description": description,
                        "username": username,
                    }
        return result

    def find_common_alerts(self, new_data, old_data):
        common_alerts = {}
        if old_data is None:
            old_data = {}
        if new_data is None:
            new_data = {}
        for key in new_data:
            if key in old_data:
                common_alerts[key] = new_data[key]
        return common_alerts

    def find_new_alerts(self, new_data, old_data):
        new_alerts = {}
        if old_data is None:
            old_data = {}
        if new_data is None:
            new_data = {}
        for key in new_data:
            if key not in old_data:
                new_alerts[key] = new_data[key]
        return new_alerts

    # 获取上次保存的last_output
    def get_alert_local(self, alert_type, file_info_path):
        return self.get_sub_items(alert_type, file_info_path, "alert_local_item")

    # 保存new_data
    def save_new_data(self, new_data, debug, base_path, slack_message_path):
        now = datetime.now()

        directory = os.path.join(
            base_path, now.strftime("%Y-%m"), now.strftime("%Y-%m-%d")
        )
        os.makedirs(directory, exist_ok=True)
        slack_directory = os.path.join(
            slack_message_path, now.strftime("%Y-%m"), now.strftime("%Y-%m-%d")
        )
        os.makedirs(slack_directory, exist_ok=True)
        file_name = now.strftime("%Y%m%d%H%M%S") + ".json"
        file_path = os.path.join(directory, file_name)
        if debug:
            print(new_data)

        with open(file_path, "w") as file:
            json.dump(new_data, file)
