#!/usr/bin/env bash
# The portal behind nginx with a self-signed stand-in certificate, against the stub master and Prometheus:
# checks the redirect, headers and sign-in, takes screenshots with e2e/shots.cjs, checks the Status page's refresh
# with e2e/status-refresh.cjs, its status bars with e2e/status-bars.cjs, the self-service password reset with
# e2e/self-reset.cjs and a private proxy granted to one account with e2e/grants.cjs, also without Prometheus,
# then removes everything.
#   e2e/run.sh <screenshot folder>     (needs Docker, openssl, and Playwright for Node in NODE_PATH)
set -euo pipefail
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY   # everything here is local
cd "$(dirname "$0")"
out=$(realpath -m "${1:?screenshot folder}")
compose() { docker compose -f compose.yml "$@"; }
trap 'compose down -v --remove-orphans >/dev/null 2>&1 || true; rm -rf run' EXIT

rm -rf run && mkdir -p run/ssl run/spool/requests run/spool/results run/spool/status
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj /CN=proxy.cvgl.lab \
  -addext subjectAltName=DNS:proxy.cvgl.lab -keyout run/ssl/Server-unsecure.pvk -out run/ssl/Server.cer 2>/dev/null
chmod 644 run/ssl/*
# the repository's nginx.conf as it is: it reaches the portal by its container name
echo "nginx -t: $(docker run --rm -v "$PWD/../../nginx/build/nginx.conf:/etc/nginx/nginx.conf:ro" -v "$PWD/run/ssl:/opt/ssl:ro" \
  nginx:1.30.6-alpine@sha256:901e593c95dc85f5a729f5765516321366ef4ac401821431d408897e95287d0a nginx -t 2>&1 | tail -1)"
python3 - <<'PY'
import datetime, json
data = json.load(open("../portal/tests/fixtures/status.json"))
data["generated_at"] = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
json.dump(data, open("run/spool/status.json", "w"), indent=2)
PY
chmod -R a+rX run/spool
chmod 777 run/spool/requests run/spool/results   # the portal writes requests and deletes results as uid 3089

compose up -d --build --quiet-pull >/dev/null
for _ in $(seq 60); do curl -sk --resolve proxy.cvgl.lab:443:127.0.0.1 -o /dev/null https://proxy.cvgl.lab/healthz && break; sleep 1; done

r() { curl -sk --resolve proxy.cvgl.lab:443:127.0.0.1 --resolve proxy.cvgl.lab:80:127.0.0.1 "$@"; }
echo "http redirect: $(r -o /dev/null -w '%{http_code} %{redirect_url}' http://proxy.cvgl.lab/access)"
echo "status:        $(r -o /dev/null -w '%{http_code}' https://proxy.cvgl.lab/)"
echo "me anonymous:  $(r -o /dev/null -w '%{http_code}' https://proxy.cvgl.lab/api/me/access)"
r -D - -o /dev/null https://proxy.cvgl.lab/ | grep -iE '^(content-security-policy|x-frame-options|x-content-type-options|referrer-policy):' | cut -c1-90
echo "answer sizes:  /api/status $(r -o /dev/null -w '%{size_download}' https://proxy.cvgl.lab/api/status) B, polled /api/status/live $(r -o /dev/null -w '%{size_download}' https://proxy.cvgl.lab/api/status/live) B"
echo "headline:      $(r https://proxy.cvgl.lab/api/status | python3 -c 'import json,sys; print(json.load(sys.stdin)["headline"]["text"])')"

# incidents in the database: one resolved, one open
incident() { compose exec -T proxy-portal python manage.py shell -c "
from django.utils import timezone; from portal.models import Incident
i = Incident.objects.create(title='$1', body='$2', component='$3', state='investigating',
                            started_at=timezone.now(), author='carol'); print(i.id)"; }
id=$(incident "Proxy restarted" "A public proxy restarted after an update." proxies | tail -1)
compose exec -T proxy-portal python manage.py shell -c "
from django.utils import timezone; from portal.models import Incident
i = Incident.objects.get(id=$id); i.state = 'resolved'; i.resolved_at = timezone.now(); i.save()"
incident "Slow downloads on one proxy" "Downloads through one public proxy are slow; the Auto-switching Proxy is not affected." proxies >/dev/null
echo "incidents:     $(r https://proxy.cvgl.lab/api/status | python3 -c 'import json,sys; print([(i["title"], i["state"]) for i in json.load(sys.stdin)["incidents"]])')"

sleep 3   # Chromium aborts with ERR_NETWORK_CHANGED while Docker is still setting up interfaces
RESOLVE="proxy.cvgl.lab 127.0.0.1" node shots.cjs https://proxy.cvgl.lab "$out" carol pw-carol
RESOLVE="proxy.cvgl.lab 127.0.0.1" node status-refresh.cjs https://proxy.cvgl.lab
RESOLVE="proxy.cvgl.lab 127.0.0.1" node status-bars.cjs https://proxy.cvgl.lab "$out"
RESOLVE="proxy.cvgl.lab 127.0.0.1" node self-reset.cjs https://proxy.cvgl.lab "$out"
RESOLVE="proxy.cvgl.lab 127.0.0.1" node grants.cjs https://proxy.cvgl.lab "$out"

stats() { compose exec -T proxy-portal python -c "import urllib.request as u; print(u.urlopen('$1/stub/stats').read().decode())"; }
echo "master stub:     $(stats http://stubs:18080)"
echo "user list:       $(compose exec -T proxy-portal python manage.py shell -c "
from portal.models import ClusterUser; print(ClusterUser.objects.count(), 'users')" | tail -1)"
echo "Prometheus stub: $(stats http://stubs:19090)"
echo "database holds the password or a token: $(compose exec -T proxy-portal python -c "
d = open('/data/portal.sqlite3', 'rb').read(); print(b'pw-carol' in d or b'v2.public' in d or b'E2e-' in d)")"
echo "files in /data: $(compose exec -T proxy-portal sh -c 'ls -l /data | tail -n +2 | awk "{print \$1, \$3, \$9}" | paste -sd ";"')"
echo "outbound from the portal: $(compose exec -T proxy-portal python -c "
import urllib.request as u
try: u.urlopen('https://1.1.1.1/', timeout=3); print('reachable')
except Exception as e: print('blocked', type(e).__name__)")"
compose logs proxy-portal 2>&1 | grep -cE 'pw-carol|E2e-' | sed 's/^/password in portal logs: /' || true

# without Prometheus (and the master): the Status page loads with monitoring data unavailable, then recovers
compose stop stubs >/dev/null 2>&1
for _ in $(seq 30); do
  [ "$(r https://proxy.cvgl.lab/api/status | python3 -c 'import json,sys; print(json.load(sys.stdin)["monitoring_available"])')" = False ] && break
  sleep 1
done
RESOLVE="proxy.cvgl.lab 127.0.0.1" node status-refresh.cjs https://proxy.cvgl.lab monitoring-down
