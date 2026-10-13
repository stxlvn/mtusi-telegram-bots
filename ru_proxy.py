"""Russian-exit SOCKS proxy selection for everything that talks to МТУСИ.

``LMS_PROXY`` is a comma-separated list in priority order; the first proxy
that reaches lk.mtuci.ru wins, so a dead home router falls over to the VPS.
"""

import os
import re

import requests

DEFAULT = "socks5h://127.0.0.1:1081,socks5h://127.0.0.1:1082"
PROBE_URL = "https://lk.mtuci.ru/auth/login"
UNREACHABLE = re.compile(r"^https://fonts\.(googleapis|gstatic)\.com/")


def proxies():
    return [p.strip() for p in os.environ.get("LMS_PROXY", DEFAULT).split(",") if p.strip()]


def _alive(proxy):
    try:
        r = requests.get(PROBE_URL, proxies={"https": proxy}, timeout=8, allow_redirects=False)
        return r.status_code < 400
    except requests.RequestException:
        return False


def pick():
    candidates = proxies()
    for proxy in candidates:
        if _alive(proxy):
            return proxy
    return candidates[0]


def browser_proxy():
    return {"server": pick().replace("socks5h://", "socks5://")}


async def prepare(context):
    """Drop requests a RU datacenter exit can't reach; the login form otherwise
    waits on them and never renders."""
    await context.route(UNREACHABLE, lambda route: route.abort())
