"""The status headline: what the probe results of the default entry ha-general mean for a user."""

from django.utils.translation import gettext as _
from django.utils.translation import ngettext

PASSING = "passing"
FAILING = "failing"
UNKNOWN = "unknown"

OK = "ok"
DEGRADED = "degraded"
DOWN = "down"


def headline(http, socks, passing, configured):
    """
    http, socks: the state of ha-general's two ports. passing, configured: its pool lines that pass on SOCKS5 and
    the configured ones (None when unknown). Returns (level, text), level one of ok, degraded, down, unknown.
    """
    if http == UNKNOWN and socks == UNKNOWN:
        return UNKNOWN, _("Status unknown: no recent probe results for the default proxy")
    if http == PASSING and socks == PASSING:
        if passing is None or configured is None or configured == 0:
            return OK, _("Default proxy available")
        failing = configured - passing  # failing or unknown: not passing
        if failing <= 0:
            return OK, ngettext("Default proxy available; its %(n)d line is passing",
                                "Default proxy available; all %(n)d lines passing", configured) % {"n": configured}
        if passing == 0:
            return DEGRADED, _("Default proxy answering, but no line passes its probe")
        if passing == 1:
            return DEGRADED, _("Default proxy available on its last passing line, without a spare")
        return DEGRADED, ngettext("Default proxy available; %(n)d line not passing, failover working",
                                  "Default proxy available; %(n)d lines not passing, failover working",
                                  failing) % {"n": failing}
    if http == FAILING and socks == FAILING:
        if passing:
            return DOWN, _("Default proxy failing while its lines pass: the HA entry itself is down")
        if passing == 0:
            return DOWN, _("Default proxy failing: no line passes its probe")
        return DOWN, _("Default proxy failing")
    if http == FAILING and socks == PASSING:
        return DEGRADED, _("HTTP entry failing, SOCKS5 still available")
    if http == PASSING and socks == FAILING:
        return DEGRADED, _("SOCKS5 entry failing, HTTP still available")
    if http == PASSING:
        return DEGRADED, _("HTTP entry available; SOCKS5 status unknown")
    if socks == PASSING:
        return DEGRADED, _("SOCKS5 entry available; HTTP status unknown")
    if http == FAILING:
        return DOWN, _("HTTP entry failing; SOCKS5 status unknown")
    return DOWN, _("SOCKS5 entry failing; HTTP status unknown")
