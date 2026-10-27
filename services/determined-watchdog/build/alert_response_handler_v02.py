import logging
import os
import signal
import sys
import traceback
from datetime import datetime, timedelta

import requests
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from alert_config import (
    TASK_KIND_DEFAULT,
    TASK_KIND_NOTEBOOK,
    TASK_KIND_SHELL,
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


CHECK_JOB_ID = "check"
DEBUG_CHECK_JOB_ID = "debug alert check"
# A check that starts late (busy host, clock step) still runs within this; later it is skipped.
MISFIRE_GRACE_SECONDS = 5 * 60
DEBUG_CHECK_SECONDS = 20  # debug mode: the alert check also runs this often

# Job errors, missed and skipped runs are logged by MainApplication.log_job_event instead.
logging.getLogger("apscheduler").addHandler(logging.NullHandler())


def next_check_time(job):
    """Next fire time of a job of a scheduler that has not started yet."""
    return job.trigger.get_next_fire_time(None, datetime.now(job.trigger.timezone))


class MainApplication:
    def __init__(self, config: Config):
        self.config = config
        self.message_notifier = MessageNotifier(config)
        self.token_manager = TokenManager(config, self.message_notifier)
        self.api_handler = APIHandler(config)
        self.DataProcessor = DataProcessor(config)
        self.now = datetime.now  # replaced in tests
        # Config.__str__ redacts the password, the Grafana token and the Slack webhook.
        print(config)

    def run(self):
        scheduler = self.build_scheduler()
        self.startup()
        log(
            f"Check schedule: {self.config.check_schedule} (every "
            f"{self.config.check_interval_minutes} min); next check at "
            f"{next_check_time(scheduler.get_job(CHECK_JOB_ID))}."
        )
        try:
            scheduler.start()  # blocks
        finally:
            # SIGTERM raises SystemExit here. A check that is running still finishes (the
            # thread pool is joined at exit, and every HTTP request has a timeout).
            if scheduler.running:
                scheduler.shutdown(wait=False)

    def startup(self):
        requests.packages.urllib3.disable_warnings()
        # Start-up token check: same renewal rules as the regular one, but silent (log
        # only) on success and on failure, so a restart posts nothing to Slack. A failed
        # renewal is retried by the next check, which notifies as usual. A start never
        # runs a check: the first one is the next fire time of the schedule.
        self.run_safely(
            "start-up token check", lambda: self.token_manager.ensure_token(notify=False)
        )
        self.self_check()

    def build_scheduler(self):
        """The scheduler with its jobs, not started."""
        # One worker: the debug job and the check never run at the same time (both save
        # the warning records).
        scheduler = BlockingScheduler(executors={"default": ThreadPoolExecutor(1)})
        scheduler.add_listener(
            self.log_job_event, EVENT_JOB_ERROR | EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES
        )
        # 定时检查: at every fire time of config.check_schedule (local timezone)
        scheduler.add_job(
            self.check_cycle,
            self.config.check_trigger(),
            id=CHECK_JOB_ID,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=MISFIRE_GRACE_SECONDS,
        )
        if self.config.is_debug:
            scheduler.add_job(
                self.debug_check_job,
                IntervalTrigger(seconds=DEBUG_CHECK_SECONDS),
                id=DEBUG_CHECK_JOB_ID,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=DEBUG_CHECK_SECONDS,
            )
        return scheduler

    def debug_check_job(self):
        self.run_safely("alert check", self.check_alerts)

    def log_job_event(self, event):
        # Replaces APScheduler's own log lines, which are dropped (NullHandler above).
        if event.code == EVENT_JOB_ERROR:
            secrets = self.config.secret_values() + [self.token_manager.token]
            exc = event.exception
            text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            log(f"{event.job_id} failed, continuing:\n{redact(text, secrets)}")
        elif event.code == EVENT_JOB_MISSED:
            log(f"{event.job_id} missed (scheduled for {event.scheduled_run_time}).")
        else:
            log(f"{event.job_id} skipped: the previous run is still running.")

    def check_cycle(self):
        # Token first, so this check's Determined calls use a renewed token.
        # Each step has its own error handling: one failure never skips the other
        # step or stops the scheduler; the next check simply tries again.
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

        shell_api_data or notebook_api_data is None if that listing failed (see _get_det_data).
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
        # The shell and notebook listings fail independently: a failure other than HTTP 401
        # only leaves that kind out of this check (None). The task listing is required (both
        # kinds need it for the container mapping), and so is at least one of the two others.
        # HTTP 401 always raises, so that fetch_det_data renews the token and retries.
        listings, errors = [], []
        for kind, get_api_data in (
            (TASK_KIND_SHELL, self.api_handler.get_shell_api_data),
            (TASK_KIND_NOTEBOOK, self.api_handler.get_notebook_api_data),
        ):
            try:
                listings.append(get_api_data(det_headers))
            except DetAPIError as e:
                if e.status_code == 401:
                    raise
                log(f"Determined API error: {e}; no {kind} is warned or killed in this check.")
                listings.append(None)
                errors.append(str(e))
        if len(errors) == len(listings):
            raise DetAPIError("; ".join(errors))
        shell_api_data, notebook_api_data = listings
        return (
            shell_api_data,
            notebook_api_data,
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
                "watchdog restarted during a check minute?); skipping this check "
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

        failed_kinds = [
            kind
            for kind, api_data in (
                (TASK_KIND_SHELL, shell_api_data),
                (TASK_KIND_NOTEBOOK, notebook_api_data),
            )
            if api_data is None
        ]
        if failed_kinds:
            # The listing's name only: an error text may echo the response body.
            failed_listings = ", ".join(f"GET {kind}s" for kind in failed_kinds)
            self.message_notifier.send_slack_warning(
                "det api miss",
                f"need update api! ({failed_listings} failed)",
                self.config.slack_webhook_url,
            )

        det_container_ids = self.api_handler.parse_api_data(
            shell_api_data, task_api_data, notebook_api_data
        )
        idle_container_ids = alert_container_ids[self.config.alert_name]

        # 解析API数据: {task_id: info} of the idle shells and notebooks
        new_data = self.DataProcessor.filter_container_by_id(
            idle_container_ids, det_container_ids
        )

        # 获取上次保存的last_output: tasks warned in the previous check
        # (at most config.warning_max_age_minutes ago). Matched by task id only: the kind
        # used for a kill comes from new_data, so records without "kind" (written before
        # notebooks were policed, shells only) still work.
        old_data = {}
        if new_data or failed_kinds:
            old_data = self.DataProcessor.load_last_output(
                self.config.alert_name, self.config.file_info_path, self.now()
            )
        # A kind whose listing failed is neither killed nor warned again in this check; its
        # warned tasks that are still idle stay tracked, so the next check can kill them.
        unchecked = self.DataProcessor.find_unchecked_alerts(
            old_data, failed_kinds, idle_container_ids
        )
        if not new_data and not unchecked:
            log(f"'{alert_container_ids}' not found in det_container_ids: {det_container_ids}.")
            return

        user_file_path = f"{self.config.base_path}/User.json"
        user_data = self.DataProcessor.read_user_info(user_file_path)

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

        # Tracked for the next check: new warnings, failed kills and the unchecked warnings.
        tracked = dict(new_alerts)
        tracked.update(failed)
        tracked.update(unchecked)
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
