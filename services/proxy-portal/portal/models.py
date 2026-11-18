from django.db import models
from django.utils import timezone
from django.utils.translation import pgettext_lazy


class Incident(models.Model):
    """A notice on the status page: an outage, degradation or maintenance, written by an admin."""

    COMPONENTS = [
        ("ha-general", pgettext_lazy("incident component", "Default proxy (ha-general)")),
        ("ha-private", pgettext_lazy("incident component", "Private entry (ha-private)")),
        ("proxies", pgettext_lazy("incident component", "Individual proxies")),
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
                "updated_at": self.updated_at.isoformat(),
                "updates": [u.as_json() for u in self.updates.all()]}

    def set_state(self, state, at=None):
        """A resolved incident gets its end time; reopening one clears it."""
        self.state = state
        if state == "resolved":
            self.resolved_at = self.resolved_at or at or timezone.now()
        else:
            self.resolved_at = None


class IncidentUpdate(models.Model):
    """A dated update of an incident, with the state it set."""

    incident = models.ForeignKey(Incident, on_delete=models.CASCADE, related_name="updates")
    state = models.CharField(max_length=32, choices=Incident.STATES)
    body = models.TextField()
    author = models.CharField(max_length=128, blank=True)
    posted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-posted_at", "-id"]

    def as_json(self):
        return {"state": self.state, "body": self.body, "posted_at": self.posted_at.isoformat()}


class AccountRequest(models.Model):
    """
    A proxy account change an admin requested: the request file the portal wrote into the applier's spool, and the
    outcome copied from the applier's status file (status/<id>.json). Never a password.
    """

    OPS = [
        ("add", pgettext_lazy("account op", "Add")),
        ("rotate", pgettext_lazy("account op", "New password")),
        ("enable", pgettext_lazy("account op", "Turn on")),
        ("disable", pgettext_lazy("account op", "Turn off")),
        ("remove", pgettext_lazy("account op", "Remove")),
        ("admin-on", pgettext_lazy("account op", "Admin account on")),
        ("admin-off", pgettext_lazy("account op", "Admin account off")),
    ]
    STATES = [
        ("pending", pgettext_lazy("request state", "Pending")),
        ("applying", pgettext_lazy("request state", "Applying")),
        ("applied", pgettext_lazy("request state", "Applied")),
        ("failed", pgettext_lazy("request state", "Failed")),
        ("unconfirmed", pgettext_lazy("request state", "Unconfirmed")),
    ]
    FINAL = ("applied", "failed", "unconfirmed")

    id = models.CharField(max_length=64, primary_key=True)
    op = models.CharField(max_length=16, choices=OPS)
    username = models.CharField(max_length=64, db_index=True)
    requested_by = models.CharField(max_length=128, db_index=True)  # the admin's Determined username
    requested_at = models.DateTimeField(default=timezone.now, db_index=True)
    # from the applier's status file
    state = models.CharField(max_length=16, choices=STATES, default="pending", db_index=True)
    stage = models.CharField(max_length=32, blank=True)
    error = models.TextField(blank=True)
    restored = models.BooleanField(null=True)
    changed = models.BooleanField(null=True)
    restarted = models.CharField(max_length=1000, blank=True)  # space-separated Compose services
    result = models.BooleanField(null=True)  # a one-time result was written
    superseded_by = models.CharField(max_length=64, blank=True)
    applied_seq = models.BigIntegerField(null=True, blank=True)  # the applier's order of live batches
    status_at = models.DateTimeField(null=True, blank=True)
    # what became of the one-time result: "" (not collected), claimed, shown, expired or unreadable
    result_state = models.CharField(max_length=16, blank=True)
    result_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-requested_at", "-id"]

    @property
    def final(self):
        return self.state in self.FINAL

    @property
    def gives_password(self):
        return self.op in ("add", "rotate")


class SignInFailure(models.Model):
    """A failed sign-in, for the limits per client address and per username. No password is stored."""

    scope = models.CharField(max_length=8)  # client or username
    key = models.CharField(max_length=256)
    at = models.DateTimeField(db_index=True)

    class Meta:
        indexes = [models.Index(fields=["scope", "key", "at"])]
