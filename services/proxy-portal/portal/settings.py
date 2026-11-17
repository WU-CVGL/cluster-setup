"""
Settings of the proxy portal. Everything an operator sets comes from the environment (PROXY_PORTAL_*);
README.md lists the variables.
"""

import os
import secrets
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
TESTING = len(sys.argv) > 1 and sys.argv[1] == "test"


def env(name, default=None):
    value = os.environ.get("PROXY_PORTAL_" + name)
    return default if value in (None, "") else value


def env_bool(name, default):
    value = env(name)
    return default if value is None else value.lower() in ("1", "true", "yes", "on")


DATA_DIR = Path(env("DATA_DIR", str(BASE_DIR / "data")))


def _secret_key():
    """The key from PROXY_PORTAL_SECRET_KEY_FILE, created (mode 0600) on the first start."""
    path = Path(env("SECRET_KEY_FILE", str(DATA_DIR / "secret_key")))
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(50)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip()
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key + "\n")
    return key


SECRET_KEY = "test-only-not-secret" if TESTING else _secret_key()
DEBUG = env_bool("DEBUG", False)

# The portal's own name: requests for any other host are refused (400).
PORTAL_HOST = env("HOST", "proxy.cvgl.lab")
ALLOWED_HOSTS = [PORTAL_HOST] + (["testserver", "127.0.0.1", "localhost"] if TESTING else [])
CSRF_TRUSTED_ORIGINS = [f"https://{PORTAL_HOST}"]

# Behind nginx, which terminates TLS and sets these headers itself.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env_bool("SSL_REDIRECT", True)
SECURE_REDIRECT_EXEMPT = [r"^healthz$"]
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
# The client address for the sign-in limits: nginx overwrites X-Real-IP with the connecting address.
CLIENT_IP_HEADER = env("CLIENT_IP_HEADER", "HTTP_X_REAL_IP")

INSTALLED_APPS = [
    "django.contrib.sessions",
    "portal",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "portal.middleware.ContentSecurityPolicy",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "portal.urls"
WSGI_APPLICATION = "portal.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.template.context_processors.csrf",
                "django.template.context_processors.i18n",
                "portal.views.portal_context",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATA_DIR / "portal.sqlite3",
        "OPTIONS": {"timeout": 20},
    }
}

if TESTING:
    # a file, so the tests can read the raw database for secrets
    import tempfile

    DATABASES["default"]["TEST"] = {"NAME": str(Path(tempfile.gettempdir()) / f"proxy-portal-test-{os.getpid()}.sqlite3")}

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "portal"}}

# Sessions: the Determined username and admin flag only, in the database, for a fixed time from sign-in.
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_NAME = "proxyportal_session"
SESSION_COOKIE_AGE = int(float(env("SESSION_HOURS", "8")) * 3600)
SESSION_COOKIE_SECURE = env_bool("SECURE_COOKIES", True)
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_SAVE_EVERY_REQUEST = False
CSRF_COOKIE_NAME = "proxyportal_csrf"
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"
LANGUAGE_COOKIE_NAME = "proxyportal_language"
LANGUAGE_COOKIE_SECURE = SESSION_COOKIE_SECURE
LANGUAGE_COOKIE_HTTPONLY = True
LANGUAGE_COOKIE_SAMESITE = "Lax"
LANGUAGE_COOKIE_AGE = 365 * 24 * 3600

LANGUAGE_CODE = "en"
LANGUAGES = [("en", "English"), ("zh-hans", "简体中文")]
LOCALE_PATHS = [BASE_DIR / "portal" / "locale"]
USE_I18N = True
USE_TZ = True
TIME_ZONE = env("TIME_ZONE", "Asia/Shanghai")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Upstreams. The master is asked once per sign-in; Prometheus with the fixed queries of portal/queries.py.
MASTER_URL = env("MASTER_URL", "http://192.168.233.6:8080").rstrip("/")
PROMETHEUS_URL = env("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")
UPSTREAM_TIMEOUT = float(env("UPSTREAM_TIMEOUT", "10"))
# In tests, every upstream must be a local stub: a request elsewhere raises.
UPSTREAM_LOOPBACK_ONLY = TESTING

# The applier's spool (applier/README.md): the portal writes requests/, reads and deletes results/, and reads
# status/ and the sanitized accounts status status.json (proxy_accounts.py status --json).
SPOOL_DIR = Path(env("SPOOL_DIR", "/srv/spool"))
STATUS_FILE = Path(env("STATUS_FILE", str(SPOOL_DIR / "status.json")))
# The applier rewrites it on every run, at least every 5 minutes; older than this, it is shown as out of date.
STATUS_FILE_MAX_AGE = int(env("STATUS_FILE_MAX_AGE", "900"))

# The nodes whose Docker daemons get an account svc-docker-<node>, offered in the admins' add form.
NODES = [n for n in env("NODES", ",".join(f"cvgl-node{i:02d}" for i in range(1, 9))).split(",") if n]
# A request still pending after this many seconds is shown as not taken by the applier (its timer runs every 5 min).
REQUEST_PENDING_WARN = int(env("REQUEST_PENDING_WARN", "360"))

# The addresses of the supplementary services VM, where the proxy entries listen.
PROXY_ADDRESSES = [
    ("private", env("PRIVATE_ADDRESS", "192.168.233.8")),
    ("campus", env("CAMPUS_ADDRESS", "10.0.1.68")),
]

# Sign-in limits: failed attempts within the window, per client address and per username.
SIGNIN_WINDOW = int(env("SIGNIN_WINDOW", "900"))
SIGNIN_MAX_PER_CLIENT = int(env("SIGNIN_MAX_PER_CLIENT", "20"))
SIGNIN_MAX_PER_USERNAME = int(env("SIGNIN_MAX_PER_USERNAME", "5"))

# A probe result older than this is unknown (the dashboards use the same 90 s).
STALE_SECONDS = 90
STATUS_CACHE_SECONDS = 15
HISTORY_CACHE_SECONDS = 300
USAGE_CACHE_SECONDS = 60

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {"portal": {"handlers": ["console"], "level": "INFO", "propagate": False}},
}
