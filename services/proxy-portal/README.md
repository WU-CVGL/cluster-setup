# Proxy portal

`https://proxy.cvgl.lab`: the status of the cluster's proxies for everyone, and for each signed-in user their own proxy access and usage. English and Chinese (from the browser's language, switchable). A Django app with SQLite, behind nginx on the supplementary services VM. How the proxies and accounts work: [docs/00](../../docs/00_Network_Proxy.md).

## Pages

| Page | JSON | Who | Content |
| :--- | :--- | :--- | :--- |
| Status `/` | `/api/status` | everyone | a headline of what the probes mean for users; `ha-general` HTTP and SOCKS5 with its proxies passing out of configured; `ha-private` and the private proxies; the public proxies; the last check; a 30-day history of `ha-general`'s probe success rate; incidents |
| My access `/access` | `/api/me/access` | signed in | the user's proxy account, the one named as their Determined username (none, on or off, admin or not), the entries it may use with their addresses on the private (`192.168.233.8`) and campus (`10.0.1.68`) networks, and client settings with `<password>` in place of the password |
| My usage `/usage` | `/api/me/usage` | signed in | the user's observed download, upload and total over 24 hours, 7 days and 30 days, and a trend chart |

- **Status** values older than 90 seconds, counting the portal's few seconds of caching, are Unknown, as is everything Prometheus does not answer; Unknown is never shown as passing, and a proxy or entry shown as Unknown shows no duration. The queries are those of the Proxy Availability dashboard, so the portal and Grafana agree. The proxies passing are counted on their SOCKS5 probe, the port the HA entry uses; a proxy not passing is failing or Unknown. Each history bar covers 24 hours; the last ends now and each earlier one a day before. A bar with probe results for less than half of its 24 hours is grey; otherwise it is green from 99 % probe success, amber from 90 %, red below.
- **My access** shows the accounts file as the applier last read it (`status.json`, rewritten after every applier run, at least every 5 minutes), never a request still waiting. `/api/me/access` also returns `status_unapplied_changes`: true when the file holds a change made with `proxy_accounts.py` since the applier's last apply. Every account may use `ha-general` and the public proxies; admin accounts also `ha-private` and the private proxies. The proxy admin flag belongs to the proxy account, not to the Determined admin role. While raw auth is off, a public proxy's own entry accepts anyone, so turning an account off stops it only at the HA entries and the private proxies; the page says so.
- **My usage** uses the Proxy Usage dashboard's per-user totals: a user is counted where they enter the proxies, summed over every entry, service accounts excluded. The totals are Prometheus `increase()` over the window ([Usage](../../docs/00_Network_Proxy.md#usage) says when they read high or low).
- **Incidents** are notices kept in the portal's database (title, text, component, state, start and end). The status page lists the open ones and those resolved in the last 30 days. See [Incidents](#incidents) to post one.

## Sign-in

Users sign in with their Determined username and password. The portal sends them once to the master's `POST /api/v1/auth/login`, refuses a wrong password (401) and an inactive user (403, or `active: false`), then at once ends the master session it opened with `POST /api/v1/auth/logout`. It keeps neither the password nor the token: the session holds the master's username, the Determined admin flag and the sign-in time.

- **The admin flag is read at sign-in.** A change on the master shows after the next sign-in.
- **Sessions** last 8 hours from sign-in, in a cookie that is `HttpOnly`, `Secure` and `SameSite=Lax`. Every form carries a CSRF token, and the portal accepts posts only from its own origin.
- **Limits:** after 5 failed sign-ins for one username or 20 from one client address within 15 minutes, further attempts are refused (429) without asking the master. A master that does not answer is not counted.
- **My pages use the session's username only.** `/api/me/*` refuses any parameter (400) and anonymous requests (401).

## What it never does

- **No PromQL from clients.** The portal sends only the fixed queries in [`portal/queries.py`](portal/queries.py), the dashboards' expressions; no endpoint takes a query, metric name, target or username for Prometheus. The per-user queries return every user and the portal keeps the session's row.
- **No passwords.** It never shows, stores or logs a proxy password; client settings carry `<password>`.
- **No Docker or accounts access.** It has no Docker socket and cannot read the accounts file. It reads `status.json` from the applier's spool through a read-only mount; `requests/` and `results/` are masked with empty in-memory folders.

## Install and upgrade

On the supplementary services VM, with the [applier](applier/README.md#install) installed (it creates the user `proxy-portal`, uid 3089, and the spool):

1. Create the data folder for the database and the secret key:

    ```sh
    sudo install -d -o 3089 -g 3089 -m 700 /var/lib/proxy-portal/data
    ```

2. Build and start it. It publishes no port: nginx reaches it as `proxy-portal:8000` on the Compose network `proxy_portal`, which only nginx and the portal join, so no client can bypass nginx and set the headers the portal trusts.

    ```sh
    cd ~/ws/cluster-setup/services
    docker compose build proxy-portal
    docker compose up -d proxy-portal
    docker compose ps proxy-portal   # healthy within a minute
    ```

    The image is built from a digest-pinned Python base with hash-pinned requirements, and runs as uid and gid 3089, the owner the applier requires of the spool's requests. On start it applies its database migrations.

3. The nginx virtual host `proxy.cvgl.lab` is in [`nginx/build/nginx.conf`](../nginx/build/nginx.conf), with the lab certificate (its `*.cvgl.lab` name covers it) and a redirect from HTTP. It resolves the portal through Docker's DNS on each request, so nginx starts and serves its other hosts while the portal is down. Rebuild and recreate nginx, which also joins it to the `proxy_portal` network: `docker compose up -d --build nginx`.

4. Add the name `proxy.cvgl.lab` where the other `*.cvgl.lab` names are: `192.168.233.8` in the nodes' and the login node's hosts files ([reference](../system-configurations/etc/hosts)), `10.0.1.68` on campus PCs ([docs/04](../../docs/04_Setup_Supplementary_Services.md#configure-nginx)).

To upgrade, pull the repository and repeat step 2. To roll back, check out the previous commit and repeat step 2; the database keeps only incidents and sign-in limits.

## Incidents

Admins post and resolve incidents in the container. `component` is one of `ha-general`, `ha-private`, `proxies`, `monitoring`, `portal`; `state` one of `investigating`, `identified`, `monitoring`, `resolved`, `maintenance`. Post one:

```sh
docker compose exec proxy-portal python manage.py shell -c "
from django.utils import timezone; from portal.models import Incident
i = Incident.objects.create(title='<title>', body='<text>', component='proxies', state='investigating',
                            started_at=timezone.now(), author='<admin>'); print(i.id)"
```

Change its state, or resolve it with its id:

```sh
docker compose exec proxy-portal python manage.py shell -c "
from django.utils import timezone; from portal.models import Incident
i = Incident.objects.get(id=<id>); i.state = 'resolved'; i.resolved_at = timezone.now(); i.save()"
```

## Configuration

Environment variables of the container (the defaults suit the VM):

| Variable | Default | |
| :--- | :--- | :--- |
| `PROXY_PORTAL_MASTER_URL` | `http://192.168.233.6:8080` | the Determined master; in compose from `services/.env` |
| `PROXY_PORTAL_PROMETHEUS_URL` | `http://prometheus:9090` | on the monitoring network |
| `PROXY_PORTAL_STATUS_FILE` | `/srv/spool/status.json` | the applier's status; older than `PROXY_PORTAL_STATUS_FILE_MAX_AGE` (900 s) it is marked out of date |
| `PROXY_PORTAL_HOST` | `proxy.cvgl.lab` | the only host name it answers |
| `PROXY_PORTAL_PRIVATE_ADDRESS`, `PROXY_PORTAL_CAMPUS_ADDRESS` | `192.168.233.8`, `10.0.1.68` | the addresses shown for the entries |
| `PROXY_PORTAL_SESSION_HOURS` | `8` | |
| `PROXY_PORTAL_SIGNIN_MAX_PER_USERNAME`, `_PER_CLIENT`, `PROXY_PORTAL_SIGNIN_WINDOW` | `5`, `20`, `900` s | |
| `PROXY_PORTAL_DATA_DIR` | `/data` | `portal.sqlite3` and `secret_key` (created on first start, mode 0600) |

In `services/.env`, `PROXY_PORTAL_SPOOL` and `PROXY_PORTAL_DATA` change the host folders mounted at `/srv/spool` and `/data`.

## Tests

```sh
cd services/proxy-portal
python3 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements.txt
.venv/bin/python manage.py compilemessages   # needs GNU gettext
.venv/bin/python manage.py test
```

The tests run against a stub master and a stub Prometheus ([`portal/tests/stubs.py`](portal/tests/stubs.py)) and refuse any other upstream. They cover sign-in (success, wrong password, inactive, the admin flag, logout, nothing kept in the database, session or logs), CSRF, the sign-in limits, `/api/me/*` refusing parameters and anonymous requests, stale and missing data as Unknown, the history, that only the fixed queries reach Prometheus and that they equal the dashboards' expressions, both languages, and the security headers.

[`e2e/run.sh`](e2e/run.sh) runs the portal behind nginx (the repository's `nginx.conf` as it is, and a self-signed stand-in certificate) against the stubs on a network without a way out, checks the redirect and headers, posts an incident with the commands of [Incidents](#incidents), and takes screenshots of every page in both languages at desktop and phone width with Playwright ([`e2e/shots.cjs`](e2e/shots.cjs)). It needs ports 80 and 443 on `127.0.0.1`, and removes its containers and volume when done.

Translations: after changing a text, run `python ../manage.py makemessages -l zh_Hans --no-location --ignore "tests/*"` in `portal/`, translate the new entries in `portal/locale/zh_Hans/LC_MESSAGES/django.po` and remove any `fuzzy` flag. The image compiles them.
