import re
from pathlib import Path

from django.test import SimpleTestCase

from .base import PortalTestCase

PO = Path(__file__).resolve().parents[1] / "locale" / "zh_Hans" / "LC_MESSAGES" / "django.po"


class TranslationFileTest(SimpleTestCase):
    def test_every_message_translated(self):
        text = PO.read_text(encoding="utf-8")
        self.assertNotIn("#, fuzzy", text)
        entries = [e for e in text.split("\n\n") if "msgid " in e]
        self.assertGreater(len(entries), 150)
        for entry in entries[1:]:  # the first is the header
            # each msgstr with its continuation lines
            strs = ["".join(re.findall(r'"(.*)"', block))
                    for block in re.findall(r'^msgstr(?:\[\d\])? (".*"(?:\n".*")*)', entry, re.M)]
            self.assertTrue(strs and all(strs), entry)


class LanguageTest(PortalTestCase):
    def test_default_english(self):
        page = self.client.get("/")
        self.assertContains(page, '<html lang="en">')
        self.assertContains(page, "Proxy status")

    def test_accept_language_chinese(self):
        page = self.client.get("/", HTTP_ACCEPT_LANGUAGE="zh-CN,zh;q=0.9,en;q=0.5")
        self.assertContains(page, '<html lang="zh-Hans">')
        self.assertContains(page, "代理服务状态")
        self.assertContains(page, "默认代理可用；代理池中 1 个代理未通过探测，故障切换正常")
        self.assertNotContains(page, "Default proxy available")
        api = self.client.get("/api/status", HTTP_ACCEPT_LANGUAGE="zh-CN").json()
        self.assertEqual(api["headline"]["text"], "默认代理可用；代理池中 1 个代理未通过探测，故障切换正常")

    def test_switch(self):
        response = self.client.post("/language", {"language": "zh-hans", "next": "/"})
        self.assertEqual(response.status_code, 302)
        self.assertContains(self.client.get("/"), "代理服务状态")
        self.client.post("/language", {"language": "en", "next": "/"})
        self.assertContains(self.client.get("/", HTTP_ACCEPT_LANGUAGE="zh-CN"), "Proxy status")

    def test_signed_in_pages_in_chinese(self):
        self.sign_in("bob")
        page = self.client.get("/access", HTTP_ACCEPT_LANGUAGE="zh-CN")
        self.assertContains(page, "我的代理接入")
        self.assertContains(page, "代理访问已<b>关闭</b>")
        self.assertContains(page, "公共代理自己的入口仍可使用")
        page = self.client.get("/usage", HTTP_ACCEPT_LANGUAGE="zh-CN")
        self.assertContains(page, "观测总量")

    def test_admin_pages_in_chinese(self):
        self.sign_in("carol")
        page = self.client.get("/admin/accounts", HTTP_ACCEPT_LANGUAGE="zh-CN")
        self.assertContains(page, "代理账号")
        self.assertContains(page, ">管理</a>")
        self.assertContains(page, "公共代理自己的入口不需要账号")
        self.assertContains(page, "svc-docker-&lt;node&gt;")
        page = self.client.get("/admin/accounts/disable?username=alice", HTTP_ACCEPT_LANGUAGE="zh-CN")
        self.assertContains(page, "关闭 alice 的代理访问")
        self.assertContains(page, "经过它们的已建立连接会断开")
        self.assertContains(self.client.get("/admin/log", HTTP_ACCEPT_LANGUAGE="zh-CN"), "变更记录")
        self.assertContains(self.client.get("/admin/incidents", HTTP_ACCEPT_LANGUAGE="zh-CN"), "维护通知不会让失败的代理显示为正常")
