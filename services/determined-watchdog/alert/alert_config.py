import os


class Config:
    def __init__(self, is_debug=True):
        self.is_debug = is_debug
        self.base_path = self.set_base_path(is_debug)
        self.file_info_name = "file_info.json"
        self.file_info_path = os.path.join(self.base_path, self.file_info_name)
        self.alert_path = os.path.join(self.base_path, "alertData")
        self.slack_message_path = os.path.join(self.base_path, "slackMessage")

        self.grafana_web = "https://grafana.lins.lab/"
        self.grafana_api = "https://grafana.lins.lab/api/alerts/"
        self.grafana_api_token = ""
        self.debug_slack_webhook_url = ""
        self.run_slack_webhook_url = ""

        self.sub_item = [
            "alert_type",
            "file_name",
            "directory",
            "created_at",
            "file_type",
        ]

        self.det_api_token = None
        # self.det_headers = {
        #     'Authorization': 'Bearer ' + self.det_api_token
        # }
        self.det_headers = None

        self.grafana_headers = {"Authorization": f"Bearer {self.grafana_api_token}"}
        self.time_function_enabled_3090 = True
        self.time_function_enabled_update = True

        self.alert_min = 16
        self.alert_update_day = 3  # 0-6 Monday -Sunday 3 :Thursday
        self.last_output = None
        # 初始化last_output
        self.last_output_path = None

        self.BateAlertKill = "3090BateTest"
        self.BateAlertWarning = "3090Warning"
        self.IdleWarning = "IdleWarning"
        self.alert_name = "3090BateTest"
        self.alert_types = [self.BateAlertKill, self.IdleWarning]

    def set_base_path(self, is_debug):
        if is_debug:
            return "/home/linsadmin/lins_services/services/supplementary/grafana/alterModel/TEST_DATA/"
        else:
            return "/home/linsadmin/lins_services/services/supplementary/grafana/alterModel/data/"
