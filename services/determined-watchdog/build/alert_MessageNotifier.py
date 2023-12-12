import json
import requests
from alert_config import Config


class MessageNotifier:
    def __init__(self, config: Config):
        self.config = config

    def send_slack_notification(
        self,
        new_data,
        container_ids_to_kill,
        user_info,
        slack_webhook_url,
    ):
        attachments = []
        for recipients, color in [
            (new_data, "warning"),
            (container_ids_to_kill, "good"),
        ]:
            fields = []
            for container_id, info in recipients.items():
                username = info["username"]
                user = user_info.get(username)
                slack_id = f"<@{user['UID']}>" if user and not self.config.is_debug else username
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

        data = {"attachments": attachments, "blocks": []}

        response = requests.post(
            slack_webhook_url,
            data=json.dumps(data),
            headers={"Content-Type": "application/json"},
            timeout=200,
        )

        if response.status_code != 200:
            raise ValueError(
                "Request to Slack returned an error %s, the response is:\n%s"
                % (response.status_code, response.text)
            )

    def send_slack_warning(
        self,
        warning_type,
        info,
        slack_webhook_url
    ):
        attachments = []
        fields = []
        field = {
            "value": f"{warning_type}",
            "title": f"{info}\n",  # Modify the title to use the description field
            "short": True,
        }
        fields.append(field)
        color = "warning"
        footer = warning_type

        attachment = {
            "fallback": f"Warning",
            "color": color,
            "title": f"Warning",
            "fields": fields,
            "footer": footer,
        }
        attachments.append(attachment)

        data = {
            "attachments": attachments,
        }

        response = requests.post(
            slack_webhook_url,
            data=json.dumps(data),
            headers={"Content-Type": "application/json"},
            timeout=200,
        )

        if response.status_code != 200:
            raise ValueError(
                "Request to Slack returned an error %s, the response is:\n%s"
                % (response.status_code, response.text)
            )
