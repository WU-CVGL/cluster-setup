"""The status headline: what the probe results of the Auto-switching Proxy ha-general mean for a user."""

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
    http, socks: the state of ha-general's two ports. passing, configured: its public proxies that pass on SOCKS5 and
    the configured ones (None when unknown). Returns (level, text), level one of ok, degraded, down, unknown.
    """
    if http == UNKNOWN and socks == UNKNOWN:
        return UNKNOWN, _("Status unknown: no recent probe results for the Auto-switching Proxy")
    if http == PASSING and socks == PASSING:
        if passing is None or configured is None or configured == 0:
            return OK, _("Auto-switching Proxy available")
        failing = configured - passing  # failing or unknown: not passing
        if failing <= 0:
            return OK, ngettext("Auto-switching Proxy available; its %(n)d public proxy is passing",
                                "Auto-switching Proxy available; all %(n)d public proxies passing",
                                configured) % {"n": configured}
        if passing == 0:
            return DEGRADED, _("Auto-switching Proxy answering, but no public proxy passes its probe")
        if passing == 1:
            return DEGRADED, _("Auto-switching Proxy available on its last passing public proxy, without a spare")
        return DEGRADED, ngettext("Auto-switching Proxy available; %(n)d public proxy not passing, "
                                  "switching still works",
                                  "Auto-switching Proxy available; %(n)d public proxies not passing, "
                                  "switching still works",
                                  failing) % {"n": failing}
    if http == FAILING and socks == FAILING:
        if passing:
            return DOWN, _("Auto-switching Proxy failing while public proxies pass")
        if passing == 0:
            return DOWN, _("Auto-switching Proxy failing: no public proxy passes its probe")
        return DOWN, _("Auto-switching Proxy failing")
    if http == FAILING and socks == PASSING:
        return DEGRADED, _("Auto-switching Proxy: HTTP failing, SOCKS5 still available")
    if http == PASSING and socks == FAILING:
        return DEGRADED, _("Auto-switching Proxy: SOCKS5 failing, HTTP still available")
    if http == PASSING:
        return DEGRADED, _("Auto-switching Proxy: HTTP available, SOCKS5 status unknown")
    if socks == PASSING:
        return DEGRADED, _("Auto-switching Proxy: SOCKS5 available, HTTP status unknown")
    if http == FAILING:
        return DOWN, _("Auto-switching Proxy: HTTP failing, SOCKS5 status unknown")
    return DOWN, _("Auto-switching Proxy: SOCKS5 failing, HTTP status unknown")
