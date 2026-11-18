"""Incidents from the admin console: create, edit, updates; shown on the status page as text, never as a probe."""

import datetime

from django.utils import timezone

from portal import queries
from portal.models import Incident, IncidentUpdate

from .base import PortalTestCase, https_client
from .stubs import vector

GENERAL = queries.STATUS["ha-general"]
XSS = '<script>alert(1)</script><img src=x onerror="alert(2)">'


class IncidentAdminTest(PortalTestCase):
    def setUp(self):
        super().setUp()
        self.sign_in("carol")

    def create(self, **fields):
        data = {"title": "Slow downloads", "component": "proxies", "state": "investigating",
                "body": "One public proxy is slow."}
        data.update(fields)
        response = self.client.post("/admin/incidents", data)
        self.assertEqual(response.status_code, 302, response.content[:2000])
        return Incident.objects.latest("id")

    def test_create_edit_update(self):
        incident = self.create()
        self.assertEqual((incident.author, incident.state, incident.resolved_at), ("carol", "investigating", None))
        self.assertLess(abs((timezone.now() - incident.started_at).total_seconds()), 60)
        page = self.client.get(f"/admin/incidents/{incident.id}")
        self.assertContains(page, "Slow downloads")

        response = self.client.post(f"/admin/incidents/{incident.id}", {
            "title": "Slow downloads on one proxy", "component": "proxies", "state": "identified",
            "body": "usca6 is slow.", "started_at": "2026-11-01T08:30"})
        self.assertEqual(response.status_code, 302)
        incident.refresh_from_db()
        self.assertEqual((incident.title, incident.state), ("Slow downloads on one proxy", "identified"))
        self.assertEqual(timezone.localtime(incident.started_at).strftime("%Y-%m-%d %H:%M"), "2026-11-01 08:30")

        self.client.post(f"/admin/incidents/{incident.id}/updates", {"state": "monitoring", "body": "Fix deployed."})
        self.client.post(f"/admin/incidents/{incident.id}/updates", {"state": "resolved", "body": "Back to normal."})
        incident.refresh_from_db()
        self.assertEqual(incident.state, "resolved")
        self.assertIsNotNone(incident.resolved_at)
        self.assertEqual([u.body for u in incident.updates.all()], ["Back to normal.", "Fix deployed."])
        self.assertEqual({u.author for u in incident.updates.all()}, {"carol"})

        status = self.client.get("/").content.decode()
        self.assertLess(status.index("Back to normal."), status.index("Fix deployed."))  # newest first
        api = self.client.get("/api/status").json()["incidents"][0]
        self.assertEqual([u["state"] for u in api["updates"]], ["resolved", "monitoring"])

        # reopening clears the end time
        self.client.post(f"/admin/incidents/{incident.id}/updates", {"state": "investigating", "body": "Again."})
        incident.refresh_from_db()
        self.assertIsNone(incident.resolved_at)

    def test_edit_with_the_start_time_cleared_keeps_it(self):
        incident = self.create(started_at="2026-11-01T08:30")
        started = incident.started_at
        response = self.client.post(f"/admin/incidents/{incident.id}", {
            "title": "Slow downloads", "component": "proxies", "state": "identified", "body": "x", "started_at": ""})
        self.assertEqual(response.status_code, 302)
        incident.refresh_from_db()
        self.assertEqual((incident.state, incident.started_at), ("identified", started))

    def test_invalid_input(self):
        response = self.client.post("/admin/incidents", {"title": "", "component": "nope", "state": "fine"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Incident.objects.count(), 0)
        incident = self.create()
        response = self.client.post(f"/admin/incidents/{incident.id}/updates", {"state": "resolved", "body": ""})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(IncidentUpdate.objects.count(), 0)

    def test_text_is_escaped(self):
        incident = self.create(title=XSS, body=XSS)
        self.client.post(f"/admin/incidents/{incident.id}/updates", {"state": "monitoring", "body": XSS})
        for path in ("/", f"/admin/incidents/{incident.id}", "/admin/incidents"):
            page = self.client.get(path).content.decode()
            self.assertNotIn("<script>alert(1)", page, path)
            self.assertNotIn("<img src=x", page, path)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page, path)
        self.assertEqual(self.client.get("/api/status").json()["incidents"][0]["title"], XSS)

    def test_maintenance_never_turns_failing_probes_green(self):
        self.prometheus.answers[GENERAL["http"]] = vector(({"protocol": "http"}, 0))
        self.prometheus.answers[GENERAL["socks"]] = vector(({"protocol": "socks"}, 0))
        before = self.client.get("/api/status").json()
        self.assertEqual(before["headline"]["level"], "down")
        self.create(title="Planned maintenance", component="ha-general", state="maintenance",
                    started_at=(timezone.localtime() - datetime.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M"))
        after = self.client.get("/api/status").json()
        self.assertEqual(after["headline"], before["headline"])
        self.assertEqual(after["entries"], before["entries"])
        self.assertEqual(after["incidents"][0]["state"], "maintenance")
        page = self.client.get("/")
        self.assertContains(page, '<span class="chip info">Maintenance</span>')
        self.assertContains(page, 'class="status-hero down"')

    def test_non_admin_cannot_post(self):
        alice = https_client()
        self.sign_in("alice", client=alice)
        self.assertEqual(alice.post("/admin/incidents", {"title": "x", "component": "proxies",
                                                         "state": "investigating"}).status_code, 403)
        self.assertEqual(Incident.objects.count(), 0)
