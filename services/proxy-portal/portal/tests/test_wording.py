"""No internal wording on any page, in either language: what a cluster user or a visitor reads is plain."""
import json
import re
from html.parser import HTMLParser

from portal import lifecycle
from portal.models import AccountRequest

from .base import PortalTestCase

EN = re.compile(r"svc-ha|svc-monitor|[Dd]efault proxy|\bpool\b|\bentry\b|\bentries\b|\bHA\b|[Rr]aw auth|applier|spool|\brender|"
                r"precheck|status\.json|accounts file|supplementary|Prometheus|[Ff]ailover|\bVM\b|xray-ha|"
                r"proxy_accounts\.py|high-availability|admin-only")
ZH = re.compile(r"默认代理|入口|执行器|代理池|池内|故障切换|虚拟机|账号文件|队列目录|高可用|仅管理员账号")
TITLE_ZH = "自动切换高可用代理"  # the section title, the owner's wording


class Text(HTMLParser):
    """The text a reader sees: text nodes outside scripts and styles, and the labels read out or shown on hover."""

    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        for name, value in attrs:
            if name in ("aria-label", "title", "placeholder", "alt") and value:
                self.parts.append(value)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def visible(html):
    parser = Text()
    parser.feed(html)
    # the reset dialog's texts are JSON for its script, and shown by it
    for blob in re.findall(r'<script id="(?:reset|status)-texts" type="application/json">(.*?)</script>', html):
        parser.parts += [v for v in json.loads(blob).values() if isinstance(v, str)]
    return " ".join(parser.parts)


class WordingTest(PortalTestCase):
    def check(self, path, client=None, **headers):
        response = (client or self.client).get(path, **headers)
        self.assertEqual(response.status_code, 200, path)
        text = visible(response.content.decode())
        zh = "zh" in headers.get("HTTP_ACCEPT_LANGUAGE", "")
        found = (ZH if zh else EN).findall(text.replace(TITLE_ZH, ""))
        self.assertEqual(found, [], f"{path} ({'zh' if zh else 'en'})")
        return text

    def pages(self, **headers):
        self.check("/", **headers)
        for user in ("alice", "bob", "frank", "carol"):  # on, off, no account, admin
            self.client.post("/signout")
            self.master.users.setdefault("frank", ("pw-frank", False, True))
            self.sign_in(user)
            self.check("/access", **headers)
            self.check("/usage", **headers)
        self.check("/admin/users", **headers)
        self.check("/admin/log", **headers)
        self.check("/admin/incidents", **headers)
        for op, name in (("add", "frank"), ("disable", "alice"), ("admin-on", "alice"), ("admin-off", "carol"),
                         ("rotate", "svc-docker-cvgl-node01"), ("disable", "svc-docker-cvgl-node01")):
            self.check(f"/admin/accounts/{op}?username={name}", **headers)
        # the protected service accounts by what they are for; a node's Docker daemon account by its name
        users = self.check("/admin/users", **headers)
        for text in (("Auto-switching proxies", "Monitoring", "svc-docker-cvgl-node01") if not headers else
                     ("自动切换代理", "监控", "svc-docker-cvgl-node01")):
            self.assertIn(text, users)

    def test_english(self):
        self.pages()

    def test_chinese(self):
        self.pages(HTTP_ACCEPT_LANGUAGE="zh-CN")

    def test_raw_auth_on_and_monitoring_down(self):
        self.write_status(lambda d: d.update(raw_auth=True, unapplied_changes=True))
        self.prometheus.down = True
        for headers in ({}, {"HTTP_ACCEPT_LANGUAGE": "zh-CN"}):
            self.check("/", **headers)
            self.client.post("/signout")
            self.sign_in("bob")
            self.check("/access", **headers)
            self.check("/usage", **headers)
            self.client.post("/signout")
            self.sign_in("carol")
            self.check("/admin/users", **headers)

    def test_request_pages(self):
        """The applier's stage and restarted services as admins read them; the stored values are the applier's."""
        self.sign_in("carol")
        for stage, restored, label, label_zh in (("precheck", True, "Checks before applying", "应用前检查"),
                                                 ("render", True, "Preparing the settings", "准备配置"),
                                                 ("restart", False, "Restarting the proxies", "重启代理")):
            response = self.client.post("/api/admin/requests", json.dumps({"op": "disable", "username": "alice",
                                                                            "confirm": "alice"}),
                                        content_type="application/json")
            request_id = response.json()["id"]
            (self.spool / "status" / f"{request_id}.json").write_text(json.dumps(
                {"id": request_id, "status": "failed", "stage": stage, "error": "exit 1", "restored": restored,
                 "restarted": ["xray-ha", "xray-jpty1-bwh-2tb"], "updated_at": "2026-01-01T00:00:00Z"}))
            text = self.check(f"/admin/requests/{request_id}")
            self.assertIn(label, text)
            self.assertIn("ha-general, ha-private, jpty1-bwh-2tb", text)
            self.assertIn(label, self.check("/admin/log"))
            self.assertIn(label_zh, self.check(f"/admin/requests/{request_id}", HTTP_ACCEPT_LANGUAGE="zh-CN"))
            self.assertEqual(AccountRequest.objects.get(id=request_id).stage, stage)
        # a stage or service this portal does not know is shown as it is
        self.assertEqual(lifecycle.stage_label("later-stage"), "later-stage")
        self.assertEqual(lifecycle.restarted_names("other xray-ha"), "other, ha-general, ha-private")
