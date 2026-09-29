from __future__ import annotations
import os
import json
import tempfile

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from alert_config import log

if TYPE_CHECKING:
    from alert_config import Config

# Format of "created_at" in file_info.json (local time).
CREATED_AT_FORMAT = "%Y-%m-%d %H:%M:%S"


def write_json_atomic(file_path, data, **dump_kwargs):
    """Write JSON through a temp file + os.replace, so a crash never leaves a truncated file."""
    directory = os.path.dirname(os.path.abspath(file_path))
    fd, tmp_path = tempfile.mkstemp(
        dir=directory, prefix=f".{os.path.basename(file_path)}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, **dump_kwargs)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, file_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


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

        write_json_atomic(file_path, data, indent=4)
        return True

    # 通用日志保存函数,返回信息用于记录在file中
    def save_json_file(self, data, base_path, folder_name, alert_type, file_type, now=None):
        if now is None:
            now = datetime.now()
        directory = os.path.join(
            base_path, folder_name, now.strftime("%Y-%m"), now.strftime("%Y-%m-%d")
        )
        os.makedirs(directory, exist_ok=True)

        file_name = f"{folder_name}_{now.strftime('%Y%m%d%H%M%S')}.json"
        file_path = os.path.join(directory, file_name)
        print(data)
        write_json_atomic(file_path, data)

        info = {
            "alert_type": alert_type,
            "file_name": file_name,
            "directory": directory,
            "created_at": now.strftime(CREATED_AT_FORMAT),
            "file_type": file_type,
        }

        return info

    def filter_container_by_id(self, container_ids, container_data):
        """Keep the shells (keyed by shell id) whose container id is in container_ids."""
        filtered_data = {}
        for shell_id, data in container_data.items():
            if data["container_id"] in container_ids:
                filtered_data[shell_id] = data
        return filtered_data

    def read_user_info(self, user_file_path):
        with open(user_file_path) as f:
            user_data = json.load(f)
        return user_data

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

    def last_record_age(self, alert_type, file_info_path, now):
        """Age of the last saved alert record, or None if there is none or it has no readable date."""
        last_output = self.get_alert_local(alert_type, file_info_path)
        if not last_output or not last_output.get("file_name"):
            return None
        try:
            return now - datetime.strptime(last_output.get("created_at"), CREATED_AT_FORMAT)
        except (TypeError, ValueError):
            return None

    def is_recent_record(self, created_at, now):
        """True if a record saved at created_at still counts as the previous check at now."""
        max_age = self.config.warning_max_age_minutes
        try:
            age = now - datetime.strptime(created_at, CREATED_AT_FORMAT)
        except (TypeError, ValueError):
            reason = "unparsable created_at %r" % (created_at,)
        else:
            if age > timedelta(minutes=max_age):
                reason = f"older than {max_age} min"
            elif age < timedelta(0):
                reason = "in the future"
            else:
                return True
        log(f"Ignoring warning record from {created_at}: {reason}; those shells are warned again.")
        return False

    def load_last_output(self, alert_type, file_info_path, now=None):
        """Shells warned in the previous check ({} if there is no usable record).

        A record older than config.warning_max_age_minutes (after downtime, or after an hour
        in which no shell was idle and nothing was saved), dated in the future, or without a
        readable created_at does not count: those shells are warned again, not killed.
        """
        last_output = self.get_alert_local(alert_type, file_info_path)
        if not last_output or not last_output.get("file_name"):
            return {}
        if now is None:
            now = datetime.now()
        if not self.is_recent_record(last_output.get("created_at"), now):
            return {}
        last_output_path = os.path.join(
            last_output.get("directory") or "", last_output["file_name"]
        )
        if not os.path.isfile(last_output_path):
            return {}
        try:
            with open(last_output_path) as f:
                old_data = json.load(f)
        except (OSError, ValueError) as e:
            log(f"Ignoring unreadable previous alert record {last_output_path}: {e}")
            return {}
        if not isinstance(old_data, dict):
            log(f"Ignoring malformed previous alert record {last_output_path}.")
            return {}
        return old_data
