import os
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional

from apscheduler.triggers.cron import CronTrigger

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

# When the check runs (crontab: minute hour day month day-of-week); WATCHDOG_SCHEDULE overrides it.
DEFAULT_CHECK_SCHEDULE = "*/30 * * * *"
# The schedule must fire at a fixed interval over this window (any weekday pattern repeats in it).
# A fixed start keeps the result independent of the start time. The analysis runs in UTC: a
# fixed interval is the same in the local timezone unless it has daylight saving time.
SCHEDULE_SAMPLE_WINDOW = timedelta(days=8)
SCHEDULE_SAMPLE_START = datetime(2026, 1, 1, tzinfo=timezone.utc)

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


def cron_trigger(schedule: str, tz=None) -> CronTrigger:
    """CronTrigger of a crontab string, in tz (default: the local timezone, as the scheduler's)."""
    try:
        return CronTrigger.from_crontab(schedule, timezone=tz)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"invalid crontab {schedule!r} ({e})") from None


def schedule_interval_minutes(schedule: str) -> int:
    """Minutes between consecutive fire times; refuses a schedule without a fixed interval.

    The warning footer promises one interval until the kill, and the age windows of the
    warning records are derived from it, so both stay true only for a periodic schedule.
    """
    trigger = cron_trigger(schedule, timezone.utc)
    end = SCHEDULE_SAMPLE_START + SCHEDULE_SAMPLE_WINDOW
    fire_times = [trigger.get_next_fire_time(None, SCHEDULE_SAMPLE_START)]
    while fire_times[-1] is not None and (len(fire_times) < 3 or fire_times[-1] < end):
        fire_times.append(trigger.get_next_fire_time(fire_times[-1], fire_times[-1]))
    if None in fire_times:
        raise ConfigError(f"crontab {schedule!r} never fires")
    gaps = {b - a for a, b in zip(fire_times, fire_times[1:])}
    if len(gaps) != 1:
        raise ConfigError(f"crontab {schedule!r} does not fire at a fixed interval")
    return int(gaps.pop().total_seconds()) // 60


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

    check_schedule: str = DEFAULT_CHECK_SCHEDULE  # crontab string, in the local timezone
    # The fields below default (None) to values derived from check_schedule.
    # Minutes between two checks: the time from a warning to its kill.
    check_interval_minutes: Optional[int] = None
    # A warning older than this (one interval plus half of one) no longer counts as the
    # previous check; the shell or notebook is warned again.
    warning_max_age_minutes: Optional[int] = None
    # A check that runs sooner than this (half an interval) after the previous saved one is
    # skipped, so that the warnings that one sent still get a full interval.
    warning_min_age_minutes: Optional[int] = None
    token_renew_hours: int = 48  # renew the Determined token when it expires sooner than this

    def __post_init__(self):
        if self.check_interval_minutes is None:
            self.check_interval_minutes = schedule_interval_minutes(self.check_schedule)
        interval = self.check_interval_minutes
        if self.warning_max_age_minutes is None:
            self.warning_max_age_minutes = interval + interval // 2
        if self.warning_min_age_minutes is None:
            self.warning_min_age_minutes = interval // 2

    def check_trigger(self, tz=None) -> CronTrigger:
        return cron_trigger(self.check_schedule, tz)

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
        check_schedule = env.get("WATCHDOG_SCHEDULE", "").strip() or DEFAULT_CHECK_SCHEDULE
        try:
            check_interval_minutes = schedule_interval_minutes(check_schedule)
        except ConfigError as e:
            raise ConfigError(f"WATCHDOG_SCHEDULE: {e}") from None
        return cls(
            det_web=env["DET_WEB_URL"],
            det_username=env["DET_USERNAME"],
            det_password=env["DET_PASSWORD"],
            grafana_web=env["GRAFANA_WEB_URL"],
            grafana_api_token=env["GRAFANA_API_TOKEN"],
            alert_name=env["GRAFANA_ALERT_NAME"],
            slack_webhook_url=slack_webhook_url,
            is_debug=is_debug,
            check_schedule=check_schedule,
            check_interval_minutes=check_interval_minutes,
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
