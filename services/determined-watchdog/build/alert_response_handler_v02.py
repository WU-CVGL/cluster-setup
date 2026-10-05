import os
import signal
import sys
import time
import traceback
from datetime import datetime, timedelta

import requests

from alert_config import (
    TASK_KIND_DEFAULT,
    Config,
    ConfigError,
    log,
    obsolete_env_names,
    redact,
)
from alert_MessageNotifier import MessageNotifier
from alert_APIHandler import APIHandler, DetAPIError
from alert_DataProcessor import DataProcessor
from alert_TokenManager import TokenManager


color_green = "\033[36m"
color_yellow = "\033[33m"
color_red = "\033[31m"
color_reset = "\033[0m"


class MainApplication:
    def __init__(self, config: Config):
        self.config = config
        self.message_notifier = MessageNotifier(config)
        self.token_manager = TokenManager(config, self.message_notifier)
        self.api_handler = APIHandler(config)
        self.DataProcessor = DataProcessor(config)
        self.now = datetime.now  # replaced in tests
        self.hourly_enabled = True
        # Config.__str__ redacts the password, the Grafana token and the Slack webhook.
        print(config)

    def run(self):
        requests.packages.urllib3.disable_warnings()
        # Start-up token check: same renewal rules as the hourly one, but silent (log
        # only) on success and on failure, so a restart posts nothing to Slack. A failed
        # renewal is retried by the next hourly check, which notifies as usual. If the
        # watchdog starts during minute alert_min, that check follows immediately.
        self.run_safely(
            "start-up token check", lambda: self.token_manager.ensure_token(notify=False)
        )
        self.self_check()

        current_time = self.now()
        is_next_color_a = True
        next_color = "\033[34m"

        while True:
            now_time = self.now()
            self.tick(now_time)

            if self.config.is_debug:
                self.run_safely("alert check", self.check_alerts)
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

    def tick(self, now_time):
        # 定时检查: once per hour, at minute alert_min
        if now_time.minute == self.config.alert_min:
            if self.hourly_enabled:
                self.hourly_enabled = False
                self.hourly_cycle()
        else:
            self.hourly_enabled = True

    def hourly_cycle(self):
        # Token first, so this hour's Determined calls use a renewed token.
        # Each step has its own error handling: one failure never skips the other
        # step or ends the loop; the next hour simply tries again.
        self.run_safely("token check", self.token_manager.ensure_token)
        self.run_safely("alert check", self.check_alerts)

    def run_safely(self, name, func):
        try:
            return func()
        except Exception:
            secrets = self.config.secret_values() + [self.token_manager.token]
            log(f"{name} failed, continuing:\n{redact(traceback.format_exc(), secrets)}")
            return None

    def check_alerts(self):
        grafana_alert = self.api_handler.get_alert_rules()
        if grafana_alert is None:
            self.message_notifier.send_slack_warning(
                warning_type="ERROR",
                info="Failed to fetch Grafana alert! Reason: empty response.",
                slack_webhook_url=self.config.slack_webhook_url,
            )
            return
        alert_total = self.api_handler.get_container_ids_by_alertname(grafana_alert)
        self.handle_alert_data_v3(alert_total)

    def self_check(self):
        log(
            "self check: Determined token %s; token file %s %s; Grafana API token %s."
            % (
                "in use" if self.token_manager.token else "MISSING",
                self.config.det_metrics_token_path,
                "present" if os.path.isfile(self.config.det_metrics_token_path) else "MISSING",
                "set" if self.config.grafana_api_token else "MISSING",
            )
        )

    def fetch_det_data(self):
        """Return (shell_api_data, notebook_api_data, task_api_data, det_headers).

        If Determined rejects the token in use with HTTP 401 (e.g. the session was
        revoked before its expiry), log in again once and, if that yields a different
        token (even one that could not be written to the token file), retry once.
        Raises DetAPIError.
        """
        det_headers = self.token_manager.det_headers()
        try:
            return self._get_det_data(det_headers)
        except DetAPIError as e:
            if e.status_code != 401 or not self.token_manager.token:
                raise
            rejected = e
        log(f"Determined rejected the token ({rejected}); renewing it.")
        before = self.token_manager.token
        self.token_manager.ensure_token(force=True)
        if not self.token_manager.token or self.token_manager.token == before:
            raise rejected  # the login failed: no new session to retry with
        det_headers = self.token_manager.det_headers()
        return self._get_det_data(det_headers)

    def _get_det_data(self, det_headers):
        # All or nothing: a check that saw only the shells would forget the warned notebooks.
        return (
            self.api_handler.get_shell_api_data(det_headers),
            self.api_handler.get_notebook_api_data(det_headers),
            self.api_handler.get_task_api_data(det_headers),
            det_headers,
        )

    def handle_alert_data_v3(self, alert_container_ids):
        """Warn about newly idle shells and notebooks; kill the ones warned in the previous check."""
        if not alert_container_ids:
            log("no alert_container_ids.")
            return

        if self.config.alert_name not in alert_container_ids:
            log(
                f"Alert '{self.config.alert_name}' is not firing "
                f"(firing: {sorted(alert_container_ids)}); nothing to do."
            )
            return

        age = self.DataProcessor.last_record_age(
            self.config.alert_name, self.config.file_info_path, self.now()
        )
        min_age = timedelta(minutes=self.config.warning_min_age_minutes)
        # Not in debug mode: its extra checks every ~20 s are not restarts, and its kills are
        # dry runs, so a warning is followed by a dry-run "Terminated" at the next check.
        if not self.config.is_debug and age is not None and timedelta(0) <= age < min_age:
            log(
                f"The previous check saved its record {int(age.total_seconds())} s ago (the "
                f"watchdog restarted during minute {self.config.alert_min}?); skipping this check "
                "so that its warnings get a full interval."
            )
            return

        try:
            shell_api_data, notebook_api_data, task_api_data, det_headers = self.fetch_det_data()
        except DetAPIError as e:
            log(f"Determined API error: {e}")
            self.message_notifier.send_slack_warning(
                "det api miss",
                "need update api!",
                self.config.slack_webhook_url,
            )
            return

        det_container_ids = self.api_handler.parse_api_data(
            shell_api_data, task_api_data, notebook_api_data
        )
        idle_container_ids = alert_container_ids[self.config.alert_name]

        # 解析API数据: {task_id: info} of the idle shells and notebooks
        new_data = self.DataProcessor.filter_container_by_id(
            idle_container_ids, det_container_ids
        )
        if not new_data:
            log(f"'{alert_container_ids}' not found in det_container_ids: {det_container_ids}.")
            return

        user_file_path = f"{self.config.base_path}/User.json"
        user_data = self.DataProcessor.read_user_info(user_file_path)

        # 获取上次保存的last_output: tasks warned in the previous check
        # (at most config.warning_max_age_minutes ago). Matched by task id only: the kind
        # used for a kill comes from new_data, so records without "kind" (written before
        # notebooks were policed, shells only) still work.
        old_data = self.DataProcessor.load_last_output(
            self.config.alert_name, self.config.file_info_path, self.now()
        )

        new_alerts = self.DataProcessor.find_new_alerts(new_data, old_data)
        container_ids_to_kill = self.DataProcessor.find_common_alerts(
            new_data, old_data
        )

        # 比较警报信息并获取需要传递给kill_containers的容器ID
        killed, failed = self.api_handler.kill_containers(
            container_ids_to_kill,
            self.config.is_debug,
            det_headers,
        )
        for task_id, info in failed.items():
            log(
                f"Kill FAILED for {info.get('kind', TASK_KIND_DEFAULT)} {task_id} "
                f"(user {info.get('username')}); keeping it tracked, retrying at the next check."
            )

        self.message_notifier.send_slack_notification(
            new_alerts,
            killed,
            user_data,
            self.config.slack_webhook_url,
        )

        # Tracked for the next check: new warnings plus failed kills.
        tracked = dict(new_alerts)
        tracked.update(failed)
        info = self.DataProcessor.save_json_file(
            tracked,
            self.config.base_path,
            "localData",
            self.config.alert_name,
            self.config.alert_name,
            now=self.now(),  # the clock that the next check's age computations use
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


def _exit_on_sigterm(signum, frame):
    # python is PID 1 in the container (exec-form CMD) and PID 1 ignores SIGTERM
    # unless it installs a handler; exit cleanly so `docker stop` does not wait 10 s.
    raise SystemExit(0)


def main():
    signal.signal(signal.SIGTERM, _exit_on_sigterm)
    try:
        config = Config.from_env()
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr, flush=True)
        return 2
    obsolete = obsolete_env_names(os.environ)
    if obsolete:
        log("Ignoring obsolete environment variable(s): " + ", ".join(obsolete))
    app = MainApplication(config)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
