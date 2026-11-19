from django import template
from django.utils.formats import number_format
from django.utils.translation import gettext as _
from django.utils.translation import pgettext

register = template.Library()

UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


@register.filter
def bytes_value(value):
    """The number part of a size in binary units, e.g. 1.25 for 1.25 GiB."""
    if value is None:
        return "—"
    size = float(value)
    for unit in UNITS:
        if size < 1024 or unit == UNITS[-1]:
            digits = 0 if unit == "B" else 2
            return number_format(round(size, digits), digits)  # number_format truncates
        size /= 1024
    return ""


@register.filter
def bytes_unit(value):
    if value is None:
        return ""
    size = float(value)
    for unit in UNITS:
        if size < 1024 or unit == UNITS[-1]:
            return unit
        size /= 1024
    return ""


@register.filter
def size(value):
    if value is None:
        return "—"
    return f"{bytes_value(value)} {bytes_unit(value)}"


@register.filter
def percent(value, digits=1):
    if value is None:
        return "—"
    # truncated, not rounded: 99.95 % never shows as 100 %
    return number_format(float(value) * 100, int(digits)) + " %"


@register.filter
def milliseconds(value):
    if value is None:
        return "—"
    return number_format(round(float(value) * 1000), 0) + " ms"


@register.filter
def age(value):
    if value is None:
        return _("Unknown")
    value = int(value)
    if value < 120:
        return _("%(n)d s ago") % {"n": value}
    if value < 7200:
        return _("%(n)d min ago") % {"n": value // 60}
    if value < 2 * 86400:
        return _("%(n)d h ago") % {"n": value // 3600}
    return _("%(n)d days ago") % {"n": value // 86400}


@register.filter
def get(mapping, key):
    try:
        return mapping.get(key)
    except AttributeError:
        return None


@register.filter
def op_label(op):
    from portal.models import AccountRequest
    return dict(AccountRequest.OPS).get(op, op)


@register.filter
def stage_label(stage):
    from portal import lifecycle
    return lifecycle.stage_label(stage)


@register.filter
def restarted_names(restarted):
    from portal import lifecycle
    return lifecycle.restarted_names(restarted)


@register.filter
def service_account_label(name):
    """What a protected service account is for, in place of its name; None for every other account."""
    return {"svc-ha": pgettext("service account", "Auto-switching proxies"),
            "svc-monitor": pgettext("service account", "Monitoring")}.get(name)


@register.filter
def incident_chip(incident):
    """A maintenance notice is information, never a passing state."""
    if incident.state == "maintenance":
        return "info"
    return "warn" if incident.open else "ok"


@register.filter
def request_level(row):
    from portal import lifecycle
    return lifecycle.level(row)
