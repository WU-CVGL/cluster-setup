from django.db import models
from django.utils.translation import pgettext_lazy


class Incident(models.Model):
    """A notice on the status page: an outage, degradation or maintenance, written by an admin."""

    COMPONENTS = [
        ("ha-general", pgettext_lazy("incident component", "Default proxy (ha-general)")),
        ("ha-private", pgettext_lazy("incident component", "Private entry (ha-private)")),
        ("proxies", pgettext_lazy("incident component", "Proxies")),
        ("monitoring", pgettext_lazy("incident component", "Monitoring")),
        ("portal", pgettext_lazy("incident component", "Portal")),
    ]
    STATES = [
        ("investigating", pgettext_lazy("incident state", "Investigating")),
        ("identified", pgettext_lazy("incident state", "Identified")),
        ("monitoring", pgettext_lazy("incident state", "Monitoring")),
        ("resolved", pgettext_lazy("incident state", "Resolved")),
        ("maintenance", pgettext_lazy("incident state", "Maintenance")),
    ]

    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    component = models.CharField(max_length=32, choices=COMPONENTS)
    state = models.CharField(max_length=32, choices=STATES)
    started_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True, blank=True)
    author = models.CharField(max_length=128, blank=True)  # the admin's Determined username
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-started_at", "-id"]

    @property
    def open(self):
        return self.state != "resolved"

    def as_json(self):
        return {"id": self.id, "title": self.title, "body": self.body, "component": self.component,
                "state": self.state, "open": self.open, "started_at": self.started_at.isoformat(),
                "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
                "updated_at": self.updated_at.isoformat()}


class SignInFailure(models.Model):
    """A failed sign-in, for the limits per client address and per username. No password is stored."""

    scope = models.CharField(max_length=8)  # client or username
    key = models.CharField(max_length=256)
    at = models.DateTimeField(db_index=True)

    class Meta:
        indexes = [models.Index(fields=["scope", "key", "at"])]
