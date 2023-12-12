import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

IS_DEBUG = os.environ["WATCHDOG_DEBUG"].capitalize() in ["TRUE", "ON", "1"]
DET_WEB_URL = os.environ["DET_WEB_URL"]
DET_USERNAME = os.environ["DET_USERNAME"]
DET_PASSWORD = os.environ["DET_PASSWORD"]
GRAFANA_WEB_URL = os.environ["GRAFANA_WEB_URL"]
GRAFANA_API_TOKEN = os.environ["GRAFANA_API_TOKEN"]
GRAFANA_ALERT_NAME = os.environ["GRAFANA_ALERT_NAME"]
PORTAINER_WEB_URL = os.environ["PORTAINER_WEB_URL"]
PORTAINER_API_TOKEN = os.environ["PORTAINER_API_TOKEN"]
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]
SLACK_WEBHOOK_URL_DEBUG = os.environ["SLACK_WEBHOOK_URL_DEBUG"]

PROMETHEUS_WEB_URL = "http://prometheus:9090"
if "PROMETHEUS_WEB_URL" in os.environ:
    PROMETHEUS_WEB_URL = os.environ["PROMETHEUS_WEB_URL"]

PROMETHEUS_CONFIG_PATH = "/etc/prometheus/prometheus.yml"
if "PROMETHEUS_CONFIG_PATH" in os.environ:
    PROMETHEUS_CONFIG_PATH = os.environ["PROMETHEUS_CONFIG_PATH"]

DATA_DIR = "/app/data"
if "DATA_DIR" in os.environ:
    DATA_DIR = os.environ["DATA_DIR"]

DATA_DIR_DEBUG = "/app/data/debug"
if "DATA_DIR_DEBUG" in os.environ:
    DATA_DIR_DEBUG = os.environ["DATA_DIR_DEBUG"]

BateAlertKill = GRAFANA_ALERT_NAME  # Alert name in Grafana
IdleWarning = "IdleWarning"  # Not used
AlertTypes = [BateAlertKill, IdleWarning]


# Pretty printing class
class PrintableConfig:
    """Printable Config defining str function"""

    def __str__(self):
        lines = [self.__class__.__name__ + ":"]
        for key, val in vars(self).items():
            if isinstance(val, Tuple):
                flattened_val = "["
                for item in val:
                    flattened_val += str(item) + "\n"
                flattened_val = flattened_val.rstrip("\n")
                val = flattened_val + "]"
            lines += f"{key}: {str(val)}".split("\n")
        return "\n    ".join(lines)


@dataclass
class Config(PrintableConfig):
    """Config class for alert service."""

    alert_name = BateAlertKill
    is_debug: bool = IS_DEBUG
    base_path: Path = Path(DATA_DIR_DEBUG) if is_debug else Path(DATA_DIR)
    file_info_name: str = "file_info.json"
    file_info_path = base_path / file_info_name
    alert_path: Path = base_path / "alertData"
    slack_message_path: Path = base_path / "slackMessage"

    det_web: str = DET_WEB_URL
    det_username: str = DET_USERNAME
    det_password: str = DET_PASSWORD
    prom_web: str = PROMETHEUS_WEB_URL
    prom_cfg_path: Path = Path(PROMETHEUS_CONFIG_PATH)
    grafana_web: str = GRAFANA_WEB_URL
    grafana_api_token: str = GRAFANA_API_TOKEN
    portainer_web: str = PORTAINER_WEB_URL
    portainer_api_token: str = PORTAINER_API_TOKEN
    slack_webhook_url: str = SLACK_WEBHOOK_URL_DEBUG if is_debug else SLACK_WEBHOOK_URL

    sub_item: List["str"] = field(default_factory=lambda: [
        "alert_type",
        "file_name",
        "directory",
        "created_at",
        "file_type",
    ])

    det_api_token: str = ""
    det_headers: str = ""

    grafana_headers: Dict[str, str] = field(default_factory=lambda: {
        "Authorization": f"Bearer {GRAFANA_API_TOKEN}"
    })
    time_function_enabled_3090: bool = True
    time_function_enabled_update: bool = True

    alert_min: int = 0
    alert_update_day: int = 3  # 0-6 Monday -Sunday 3 :Thursday
    alert_types = AlertTypes
    last_output: Dict[str, str] = field(default_factory=dict)
    # 初始化last_output
    last_output_path: Path = field(default_factory=Path)
