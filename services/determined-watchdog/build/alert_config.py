import os
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional

# Values of WATCHDOG_DEBUG that enable debug mode (compared case-insensitively).
TRUE_VALUES = ("1", "true", "yes", "on")

REQUIRED_ENV = (
    "WATCHDOG_DEBUG",
    "DET_WEB_URL",
    "DET_USERNAME",
    "DET_PASSWORD",
    "GRAFANA_WEB_URL",
    "GRAFANA_API_TOKEN",
    "GRAFANA_ALERT_NAME",
    "SLACK_WEBHOOK_URL",
    "SLACK_WEBHOOK_URL_DEBUG",
)

# Used by older versions (prometheus.yml rewrite + Prometheus reload/restart).
# Prometheus now reads the token from the shared token file
# (DETERMINED_METRICS_TOKEN_FILE), so these are ignored if set.
OBSOLETE_ENV = (
    "PORTAINER_WEB_URL",
    "PORTAINER_API_TOKEN",
    "PROMETHEUS_CONFIG_PATH",
    "PROMETHEUS_WEB_URL",
)

DEFAULT_DATA_DIR = "/app/data"
DEFAULT_DATA_DIR_DEBUG = "/app/data/debug"
# Shared with Prometheus: host DET_METRICS_SECRETS_DIR from services/.env (default
# services/prometheus/secrets/) is mounted at
# /run/determined-metrics (read-write here, read-only in Prometheus, whose det-master
# job reads the token through authorization.credentials_file).
DEFAULT_DETERMINED_METRICS_TOKEN_FILE = "/run/determined-metrics/token"

# (connect, read) timeout in seconds, used for every HTTP request.
HTTP_TIMEOUT = (10, 60)

# Kinds of Determined task that the watchdog polices, with their Slack labels. The kind is
# saved as "kind" in the warning records; records written before notebooks were policed have
# no "kind" and only hold shells (TASK_KIND_DEFAULT).
TASK_KIND_SHELL = "shell"
TASK_KIND_NOTEBOOK = "notebook"
TASK_KIND_DEFAULT = TASK_KIND_SHELL
TASK_KIND_LABELS = {TASK_KIND_SHELL: "Shell", TASK_KIND_NOTEBOOK: "JupyterLab"}

REDACTED = "<redacted>"
_SECRET = {"secret": True}


class ConfigError(Exception):
    """Raised when the environment does not describe a usable configuration."""


def parse_bool(value: Optional[str]) -> bool:
    """WATCHDOG_DEBUG parsing: 1/true/yes/on (any case) mean True, anything else False."""
    return value is not None and value.strip().lower() in TRUE_VALUES


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def redact(text: str, secrets: Iterable[Optional[str]]) -> str:
    """Replace every occurrence of the given secret values in text."""
    text = str(text)
    # Longest first, so a secret that contains another one is replaced whole.
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return text


@dataclass(repr=False)
class Config:
    """Config class for alert service. Build it with Config.from_env()."""

    det_web: str
    det_username: str
    det_password: str = field(metadata=_SECRET)
    grafana_web: str = ""
    grafana_api_token: str = field(default="", metadata=_SECRET)
    alert_name: str = "IdleKillAlert"  # Alert name in Grafana
    # Already resolved: SLACK_WEBHOOK_URL_DEBUG in debug mode, SLACK_WEBHOOK_URL otherwise.
    slack_webhook_url: str = field(default="", metadata=_SECRET)
    is_debug: bool = False
    base_path: Path = Path(DEFAULT_DATA_DIR)
    det_metrics_token_path: Path = Path(DEFAULT_DETERMINED_METRICS_TOKEN_FILE)
    file_info_name: str = "file_info.json"

    sub_item: List[str] = field(default_factory=lambda: [
        "alert_type",
        "file_name",
        "directory",
        "created_at",
        "file_type",
    ])

    alert_min: int = 0  # the hourly check runs at this minute of every hour
    # A warning older than this (one hourly interval plus margin) no longer counts as the
    # previous check; the shell or notebook is warned again.
    warning_max_age_minutes: int = 90
    # A check that runs sooner than this after the previous saved one (a restart during minute
    # alert_min) is skipped, so that the warnings it just sent still get a full interval.
    warning_min_age_minutes: int = 30
    token_renew_hours: int = 48  # renew the Determined token when it expires sooner than this

    @property
    def file_info_path(self) -> Path:
        return self.base_path / self.file_info_name

    @property
    def grafana_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.grafana_api_token}"}

    def secret_values(self) -> List[str]:
        values = [getattr(self, f.name) for f in fields(self) if f.metadata.get("secret")]
        return [v for v in values if v]

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "Config":
        env = os.environ if environ is None else environ
        missing = [name for name in REQUIRED_ENV if name not in env]
        if missing:
            raise ConfigError(
                "missing required environment variable(s): " + ", ".join(missing)
            )
        is_debug = parse_bool(env["WATCHDOG_DEBUG"])
        if is_debug:
            base_path = env.get("DATA_DIR_DEBUG", DEFAULT_DATA_DIR_DEBUG)
            slack_webhook_url = env["SLACK_WEBHOOK_URL_DEBUG"]
        else:
            base_path = env.get("DATA_DIR", DEFAULT_DATA_DIR)
            slack_webhook_url = env["SLACK_WEBHOOK_URL"]
        return cls(
            det_web=env["DET_WEB_URL"],
            det_username=env["DET_USERNAME"],
            det_password=env["DET_PASSWORD"],
            grafana_web=env["GRAFANA_WEB_URL"],
            grafana_api_token=env["GRAFANA_API_TOKEN"],
            alert_name=env["GRAFANA_ALERT_NAME"],
            slack_webhook_url=slack_webhook_url,
            is_debug=is_debug,
            base_path=Path(base_path),
            det_metrics_token_path=Path(
                env.get(
                    "DETERMINED_METRICS_TOKEN_FILE", DEFAULT_DETERMINED_METRICS_TOKEN_FILE
                )
            ),
        )

    def __str__(self):
        lines = [self.__class__.__name__ + ":"]
        for f in fields(self):
            val = getattr(self, f.name)
            if f.metadata.get("secret"):
                val = REDACTED if val else "<unset>"
            lines.append(f"{f.name}: {val}")
        return "\n    ".join(lines)

    __repr__ = __str__


def obsolete_env_names(environ: Optional[Mapping[str, str]] = None) -> List[str]:
    env = os.environ if environ is None else environ
    return [name for name in OBSOLETE_ENV if name in env]
