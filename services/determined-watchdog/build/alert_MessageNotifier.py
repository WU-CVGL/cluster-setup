import json
from urllib.parse import urlsplit

import requests

from alert_config import HTTP_TIMEOUT, Config, log, redact


class MessageNotifier:
    """Slack delivery is best-effort: failures are logged, never raised."""

    def __init__(self, config: Config):
        self.config = config

    def _secrets(self, slack_webhook_url):
        secrets = list(self.config.secret_values())
        for url in (slack_webhook_url, self.config.slack_webhook_url):
            if url:
                secrets.append(url)
                # Exception texts often quote only the path (".../services/T../B../xxx").
                path = urlsplit(url).path
                if len(path) > 1:
                    secrets.append(path)
        return secrets

    def _post(self, data, slack_webhook_url) -> bool:
        try:
            response = requests.post(
                slack_webhook_url,
                data=json.dumps(data),
                headers={"Content-Type": "application/json"},
                timeout=HTTP_TIMEOUT,
            )
            if response.status_code != 200:
                log(
                    "Slack delivery failed: HTTP %s, the response is: %s"
                    % (response.status_code, redact(response.text[:200], self._secrets(slack_webhook_url)))
                )
                return False
            return True
        except Exception as e:  # never let Slack take the watchdog down
            log(
                "Slack delivery failed (%s): %s"
                % (type(e).__name__, redact(str(e), self._secrets(slack_webhook_url)))
            )
            return False

    def send_slack_notification(
        self,
        new_data,
        container_ids_to_kill,
        user_info,
        slack_webhook_url,
    ) -> bool:
        """Warn about new idle shells and report the ones that were terminated.

        Only non-empty attachments are sent; nothing is posted if both are empty.
        """
        try:
            attachments = []
            for recipients, color in [
                (new_data, "warning"),
                (container_ids_to_kill, "good"),
            ]:
                if not recipients:
                    continue
                fields = []
                for shell_id, info in recipients.items():
                    username = info.get("username")
                    user = user_info.get(username) if isinstance(user_info, dict) else None
                    uid = user.get("UID") if isinstance(user, dict) else None
                    slack_id = f"<@{uid}>" if uid and not self.config.is_debug else username
                    description = info.get("description", "")
                    field = {"value": slack_id, "title": f"{description}\n", "short": True}
                    fields.append(field)

                footer = (
                    "Your container will be released in 60 minutes. Please check your task!!!"
                    if color == "warning"
                    else "These GPU containers have been released"
                )

                attachment = {
                    "fallback": "Warning" if color == "warning" else "Terminated",
                    "color": color,
                    "title": "Warning" if color == "warning" else "Terminated",
                    "fields": fields,
                    "footer": footer,
                }
                attachments.append(attachment)

            if not attachments:
                log("Nothing to report to Slack.")
                return False

            data = {"attachments": attachments, "blocks": []}
        except Exception as e:
            log(f"Failed to build the Slack notification ({type(e).__name__}): {e}")
            return False
        return self._post(data, slack_webhook_url)

    def send_slack_warning(
        self,
        warning_type,
        info,
        slack_webhook_url
    ) -> bool:
        attachments = []
        fields = []
        field = {
            "value": f"{warning_type}",
            "title": f"{info}\n",
            "short": True,
        }
        fields.append(field)
        color = "warning"
        footer = f"{warning_type}"

        attachment = {
            "fallback": "Warning",
            "color": color,
            "title": "Warning",
            "fields": fields,
            "footer": footer,
        }
        attachments.append(attachment)

        data = {
            "attachments": attachments,
        }
        return self._post(data, slack_webhook_url)
