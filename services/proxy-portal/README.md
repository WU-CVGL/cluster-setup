# Proxy portal

`https://proxy.cvgl.lab`: the status of the cluster's proxies for everyone, for each signed-in user their own proxy access and usage, and for admins an [admin console](#admin-console) for proxy accounts and incidents. English and Chinese (from the browser's language, switchable). A Django app with SQLite, behind nginx on the supplementary services VM. How the proxies and accounts work: [docs/00](../../docs/00_Network_Proxy.md).

## Pages

| Page | JSON | Who | Content |
| :--- | :--- | :--- | :--- |
| Status `/` | `/api/status`, `/api/status/live` | everyone | a headline of what the probes mean for users; the auto-switching high-availability proxies `ha-general` and `ha-private`, with their proxies passing out of configured, and each proxy, public and private, in one table layout: ports, HTTP and SOCKS5 status, latency, 24-hour success, last check and [status bars](#status-bars); the last check; incidents |
| My access `/access` | `/api/me/access`, `/api/me/reset` | signed in | the user's proxy account, the one named as their Determined username (none, on or off, admin or not), the entries it may use with their addresses on the private (`192.168.233.8`) and campus (`10.0.1.68`) networks, client settings with `<password>` in place of the password, and [a password reset](#password-reset) |
| My usage `/usage` | `/api/me/usage` | signed in | the user's observed download, upload and total over 24 hours, 7 days and 30 days, and a trend chart |
| Users `/admin/users` | `/api/admin/users`, `/api/admin/accounts`, `/api/admin/requests` | admins | every cluster user with their Determined flags, proxy account, private access and pending request, and the service accounts apart; filters and search; add, new password, turn on or off, admin account on or off, remove. `/admin/accounts` redirects here |
| Request `/admin/requests/<id>` | `/api/admin/requests/<id>` | admins | one request's state, updated while it runs, and for an add or new password the one-time password |
| Change log `/admin/log` | `/api/admin/requests?username=&requested_by=&op=&state=` | admins | every request, who made it and when, and its outcome; filters |
| Incidents `/admin/incidents` | | admins | create and edit incidents and post updates |

- **Status** values older than 90 seconds, counting the portal's few seconds of caching, are Unknown, as is everything Prometheus does not answer; Unknown is never shown as passing, and a proxy or entry shown as Unknown shows no duration. The open page polls `/api/status/live`, the values of `/api/status` that change between reloads (without the bars and the incidents), every 30 seconds and when its tab becomes visible again, drops a poll with no answer after 15 seconds, and ages the results it shows every second, from the last answer it got: when polls fail, results turn Unknown as they pass 90 seconds. The status bars and the incidents update on reload. The queries are those of the Proxy Availability dashboard, so the portal and Grafana agree. The proxies passing are counted on their SOCKS5 probe, the port the HA entry uses; a proxy not passing is failing or Unknown.
- **My access** shows the accounts file as the applier last read it (`status.json`, rewritten after every applier run, at least every 5 minutes), never a request still waiting. `/api/me/access` also returns `status_unapplied_changes`: true when the file holds a change made with `proxy_accounts.py` since the applier's last apply. Every account may use `ha-general` and the public proxies; admin accounts also `ha-private` and the private proxies. The proxy admin flag belongs to the proxy account, not to the Determined admin role. While raw auth is off, a public proxy's own entry accepts anyone, so turning an account off stops it only at the HA entries and the private proxies; the page says so. The [open proxies'](../../docs/00_Network_Proxy.md#open-proxies) own entries accept anyone also while raw auth is on: My access shows them as "No account needed", `/api/me/access` returns them as `open_proxies`, and the Status page tags every proxy whose own entry needs no account (`/api/status` returns each proxy's `open` and `account_required`, null without the applier's status).
- **My usage** uses the Proxy Usage dashboard's per-user totals: a user is counted where they enter the proxies, summed over every entry, service accounts excluded. The totals are Prometheus `increase()` over the window ([Usage](../../docs/00_Network_Proxy.md#usage) says when they read high or low).
- **Incidents** are notices kept in the portal's database (title, text, component, state, start and end, and dated updates). The status page lists the open ones and those resolved in the last 30 days, as plain text. They never change a probe result: a maintenance notice does not turn a failing proxy green.

## Sign-in

Users sign in with their Determined username and password. The portal sends them once to the master's `POST /api/v1/auth/login`, refuses a wrong password (401) and an inactive user (403, or `active: false`), then ends the master session it opened with `POST /api/v1/auth/logout` (for an admin, right after reading the user list). It keeps neither the password nor the token: the session holds the master's username, the Determined admin flag and the sign-in time.

- **The admin flag is read at sign-in.** A change on the master shows after the next sign-in.
- **The user list is read at an admin's sign-in.** Before the logout, the portal calls `GET /api/v1/users` with the admin's token and replaces its list of cluster users (username, active, admin, and the time) with the answer; it keeps nothing else and never the token. If that call fails, answers anything unexpected (repeated names, more than 20,000 users) or takes more than 15 seconds in all, the sign-in goes on, the previous list stays and a warning is logged. Other users' sign-ins never call it.
- **Sessions** last 8 hours from sign-in, in a cookie that is `HttpOnly`, `Secure` and `SameSite=Lax`. Every form carries a CSRF token, and the portal accepts posts only from its own origin.
- **Limits:** after 5 failed sign-ins for one username or 20 from one client address within 15 minutes, further attempts are refused (429) without asking the master. A master that does not answer is not counted.
- **My pages use the session's username only.** `/api/me/*` refuses any parameter (400) and anonymous requests (401). `/api/me/reset/<id>` answers only the user who made that reset (403 for anyone else, admins included).

## What it never does

- **No PromQL from clients.** The portal sends only the fixed queries in [`portal/queries.py`](portal/queries.py), the dashboards' expressions; no endpoint takes a query, metric name, target or username for Prometheus. The per-user queries return every user and the portal keeps the session's row.
- **No kept passwords.** It never stores or logs a proxy password, and takes none from a client. The only password it shows is a new one, once, to the user or admin who requested it ([One-time passwords](#one-time-passwords)); client settings carry `<password>`.
- **No Docker or accounts access.** It has no Docker socket and cannot read the accounts file. The applier's spool is mounted read-only, except `requests/`, where it writes requests, and `results/`, where it reads and deletes one-time passwords ([applier README](applier/README.md#spool)).

## Password reset

On My access, a user whose account exists and has proxy access on gets **Reset proxy password**. It opens a dialog that says what happens: the account gets a new random password, the old one stops working, so every client that uses it needs the new one, and applying restarts the HA entries (also the private proxies for an admin account, and every proxy but the open ones while raw auth is on), so established connections drop. On confirmation the portal writes a `rotate` request for the session's own account, recorded with the user as the requester; the request takes no parameter. The dialog follows the request with the [request states](#request-states) and, once the change is live, shows the password once to that user, with a copy button ([One-time passwords](#one-time-passwords)). Closing the dialog and opening it again, or reloading, resumes a reset still running or a password not yet shown.

- **Refused** with a reason for a disabled account, while any request for the account is waiting or applying, and after `PROXY_PORTAL_SELF_RESET_MAX_PER_HOUR` (3) resets by the user within an hour. Users without an account and service accounts see no button.
- **Only the user** can follow the reset and see the password: admins see the request in the change log, never its password.
- **My access** still shows the applied state only.

## Install and upgrade

On the supplementary services VM, with the [applier](applier/README.md#install) installed (it creates the user `proxy-portal`, uid 3089, and the spool):

1. Create the data folder for the database and the secret key:

    ```sh
    sudo install -d -o 3089 -g 3089 -m 700 /var/lib/proxy-portal/data
    ```

2. Build and start it. It publishes no port: nginx reaches it as `proxy-portal:8000` on the Compose network `proxy_portal`, which only nginx and the portal join. It reaches Prometheus and the master over `proxy_portal_upstream`, which only it and Prometheus join. It is on no network a proxy is on: the proxies send private addresses out directly, so a portal on `grafana_monitor` could be reached through a proxy. So no client can bypass nginx, its limits and the headers the portal trusts. Prometheus joins `proxy_portal_upstream` only when it is recreated, so the commands recreate it first; without it every status is Unknown.

    ```sh
    cd ~/ws/cluster-setup/services
    docker pull "$(sed -n 's/^FROM \([^ ]*\).*/\1/p' proxy-portal/Dockerfile | head -1)"
    docker compose build proxy-portal
    docker compose up -d prometheus     # joins proxy_portal_upstream
    docker compose up -d proxy-portal
    docker compose ps proxy-portal   # healthy within a minute
    ```

    The image is built from a digest-pinned Python base with hash-pinned requirements, and runs as uid and gid 3089, the owner the applier requires of the spool's requests. On start it applies its database migrations.

    The `docker pull` line fetches the pinned base first: the build looks it up without the Docker daemon's proxy and fails while Docker Hub is unreachable, whereas `docker pull` uses that proxy. When downloads from PyPI are unreliable, pass a proxy to the build; any public proxy works, for example:

    ```sh
    docker compose build \
      --build-arg HTTP_PROXY=http://192.168.233.8:58889 --build-arg HTTPS_PROXY=http://192.168.233.8:58889 \
      --build-arg NO_PROXY=localhost,127.0.0.1 proxy-portal
    ```

3. The nginx virtual host `proxy.cvgl.lab` is in [`nginx/build/nginx.conf`](../nginx/build/nginx.conf), with the lab certificate (its `*.cvgl.lab` name covers it) and a redirect from HTTP. It resolves the portal through Docker's DNS on each request, so nginx starts and serves its other hosts while the portal is down. Rebuild and recreate nginx, which also joins it to the `proxy_portal` network: `docker compose up -d --build nginx`.

4. Add the name `proxy.cvgl.lab` where the other `*.cvgl.lab` names are: `192.168.233.8` in the nodes' and the login node's hosts files ([reference](../system-configurations/etc/hosts)), `10.0.1.68` on campus PCs ([docs/04](../../docs/04_Setup_Supplementary_Services.md#configure-nginx)).

To upgrade, pull the repository and repeat step 2. To roll back, check out the previous commit and repeat step 2; the database keeps only incidents, account requests with their outcome, and sign-in limits.

## Admin console

**Admins** are the users whose Determined admin flag was true when they signed in. The flag is read only at sign-in: after a change on the master, the user signs out and in again. It is not the proxy account's admin flag. Every admin page and API checks it on the server: anonymous users are sent to sign-in (pages) or get 401 (APIs), other users get 403. An **Admin** link in the header appears for admins only.

### Users

The Users page lists every cluster user of the list read at the last admin sign-in ([Sign-in](#sign-in)), and every user account of the accounts file, including one whose name is not in that list (marked Not in the user list). For each: the Determined active and admin flags, the proxy account as the applier last read the accounts file (`status.json`: none, on or off), private access (an admin account) and a pending request. "User list as of <time>" says when the list was read. Filters show the users without an account, with proxy access off, with private access, and those deactivated in Determined whose proxy access is still on; search matches the name. A name that cannot be a proxy account name gets no add. The service accounts are listed apart: `svc-ha` and `svc-monitor` cannot be changed here; a `svc-docker-<node>` account can get a new password, be turned off or on, or be removed, and is never an admin account. Each change is a request, sent after a confirmation page; rotate, disable, remove and admin-off also need the account name typed:

| Action | Request | Effect |
| :--- | :--- | :--- |
| Add | `add` | a new account with a random password, shown once; it may use `ha-general` and the public proxies. A person's account is named as their Determined username; a node's Docker daemon uses `svc-docker-<node>` |
| New password | `rotate` | a new random password, shown once; the old one stops working |
| Turn off / on | `disable` / `enable` | proxy access off or on; the account and its password are kept |
| Admin account on / off | `admin-on` / `admin-off` | the account may use, or no longer use, `ha-private` and the private proxies; users only |
| Remove | `remove` | the account and its password are deleted |

Names are checked with the rules of `proxy_accounts.py` before anything is written. `svc-ha` and `svc-monitor` cannot be changed here (`proxy_accounts.py` only). The portal writes `requests/<id>.json` (`{"id", "op", "username"}`, a new UUID per request) under a temporary name and renames it, and records the request with the admin's username and time. It never changes the accounts file itself.

**Restart impact.** Every change restarts the affected entries: `xray-ha`; also the private proxies for a change to an admin account; also every proxy but the [open ones](../../docs/00_Network_Proxy.md#open-proxies) while raw auth is on. Established connections through them drop, and new ones fail for a few seconds. The confirmation page says which.

**Raw auth.** While raw auth is off, turning an account off stops only `ha-general`, `ha-private` and the private proxies: a public proxy's own entry needs no account and still accepts that user. While raw auth is on, the [open proxies'](../../docs/00_Network_Proxy.md#open-proxies) own entries still do. The list, the confirmation page and the request page say so.

### Request states

Read from the applier's `status/<id>.json` ([statuses](applier/README.md#statuses)); the request page updates itself while a request runs.

| State | Meaning |
| :--- | :--- |
| Pending | written, not taken yet; nothing has changed, so the old access still works. After 6 minutes the page says the applier may not be running |
| Applying | being applied; the entries restart. A revoke is not confirmed yet: the old access may still work |
| Applied | live and verified; "nothing to change" when the account was already in that state (the entries still restart when the same apply changed something else) |
| Failed | not applied, with the stage and error. The previous state is back (after a failure at `swap`, `restart`, `verify` or `commit` the rollback restarted the entries, so established connections dropped), or, when the rollback did not finish, the entries may be down or mixed: check `journalctl -u proxy-apply` on the VM. The portal keeps reading such a request's status, and the page updates itself, until the applier reports the rollback's outcome |
| Unconfirmed | live, and every login that must be refused is refused, but the logins that must pass got no answer in time (proxies or the check target down) |

**My access** shows the applied state only, never a pending request. The change log lists every request with its outcome, filterable by account, admin, operation and state.

### One-time passwords

For an add or a new password, the applier writes the password to `results/<id>.json` once the change is live (applied or unconfirmed), readable only by uid 3089. The admin who made the request (or, for a [password reset](#password-reset), the user, in the dialog) clicks **Show the password once**: the portal reads the file, deletes it at once and shows the password on a page that says it will not be shown again, with a copy button. It is never stored in the database, session, cache or logs. Another admin cannot see it. Afterwards, or once the applier has deleted an uncollected result (after 10 minutes), or when a later live add, new password or removal of the account replaced it (in the same applier run or a later one; "later" is the order in which the applier applied them, `applied_seq` in their statuses, never the order of the requests), the page says so; request a new password to get another. The portal does not see a password changed or an account removed with `proxy_accounts.py`.

### Incidents

**Incidents** in the admin console: title, affected component (`ha-general`, `ha-private`, `proxies`, `monitoring`, `portal`), state (investigating, identified, monitoring, resolved, maintenance), start time and text. **Post an update** adds a dated note and sets the state: resolved sets the end time, any other state reopens it. The status page shows each incident with its updates, newest first, as plain text.

## Configuration

Environment variables of the container (the defaults suit the VM):

| Variable | Default | |
| :--- | :--- | :--- |
| `PROXY_PORTAL_MASTER_URL` | `http://192.168.233.6:8080` | the Determined master; in compose from `services/.env` |
| `PROXY_PORTAL_PROMETHEUS_URL` | `http://prometheus:9090` | on the `proxy_portal_upstream` network |
| `PROXY_PORTAL_SPOOL_DIR` | `/srv/spool` | the applier's spool: `requests/`, `results/`, `status/` |
| `PROXY_PORTAL_STATUS_FILE` | `/srv/spool/status.json` | the applier's status; older than `PROXY_PORTAL_STATUS_FILE_MAX_AGE` (900 s) it is marked out of date |
| `PROXY_PORTAL_NODES` | `cvgl-node01`…`cvgl-node08` | comma-separated; the add form offers `svc-docker-<node>` for those without an account |
| `PROXY_PORTAL_HOST` | `proxy.cvgl.lab` | the only host name it answers |
| `PROXY_PORTAL_PRIVATE_ADDRESS`, `PROXY_PORTAL_CAMPUS_ADDRESS` | `192.168.233.8`, `10.0.1.68` | the addresses shown for the entries |
| `PROXY_PORTAL_SESSION_HOURS` | `8` | |
| `PROXY_PORTAL_SIGNIN_MAX_PER_USERNAME`, `_PER_CLIENT`, `PROXY_PORTAL_SIGNIN_WINDOW` | `5`, `20`, `900` s | |
| `PROXY_PORTAL_SELF_RESET_MAX_PER_HOUR` | `3` | password resets per user per hour |
| `PROXY_PORTAL_DATA_DIR` | `/data` | `portal.sqlite3` and `secret_key` (created on first start, mode 0600) |

In `services/.env`, `PROXY_PORTAL_SPOOL` and `PROXY_PORTAL_DATA` change the host folders mounted at `/srv/spool` and `/data`.

## Status bars

Every row of the Status page, the two high-availability proxies and each proxy, has status bars: 96 hourly bars for the last 96 hours, or 30 daily bars for the last 30 days. A switch above the tables changes every row; the browser keeps the choice (in `localStorage`; 96 hours when it has none or cannot keep it).

- **Periods.** The last bar ends at the last full hour, so a failure in the current hour shows in the status columns first and in the bars once the hour ends. Hourly bars are clock hours; each daily bar covers the 24 hours before its end, a day apart. Times are in the portal's time zone.
- **Colour.** A bar takes the worse of the HTTP and SOCKS5 probe success rates in its period: green from 99 %, amber from 90 %, red below. It is grey when probe results cover less than half of the period (a protocol without results covers none of it) or when there is no data.
- **Tooltip and keyboard.** Each bar's period, state, success rate and coverage show on hover and on focus, and are its accessible name. Each bar list is one tab stop; the arrow keys, Home and End move between its bars, and Esc hides the tooltip, also one shown on hover. A focused bar keeps its tooltip while the page scrolls.
- **Queries.** Four range queries, one per value (success rate and coverage) and bar size, each for every row at once and grouped by entry and protocol, as on the dashboard's Status bars row. They are cached for 5 minutes per bar size and end hour. With 14 rows, `/api/status` is about 45 kB, the page about 260 kB and the polled `/api/status/live` about 5.5 kB, uncompressed.
- **API.** `/api/status` returns the bars under `bars.hours` and `bars.days`: `end` (Unix time), `step_seconds`, `count`, and per row lists of `state` (`ok`, `degraded`, `bad`, `unknown`), `success_rate` and `coverage`, oldest first. Each entry and proxy also has `ports` (`http`, `socks`) from the applier's status, `null` when it is unavailable.

## Tests

```sh
cd services/proxy-portal
python3 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements.txt
.venv/bin/python manage.py compilemessages   # needs GNU gettext
.venv/bin/python manage.py test
```

The tests run against a stub master and a stub Prometheus ([`portal/tests/stubs.py`](portal/tests/stubs.py)) and refuse any other upstream. They cover sign-in (success, wrong password, inactive, the admin flag, logout, nothing kept in the database, session or logs), CSRF, the sign-in limits, `/api/me/*` refusing parameters and anonymous requests, stale and missing data as Unknown, the status bars (colour and grey rules, the worse protocol, missing rows, labels in local time, the switch's server-rendered default, the cost with 14 rows), the ports, that only the fixed queries reach Prometheus and that they equal the dashboards' expressions, both languages, and the security headers. For the admin console: 401, redirect and 403 on every admin URL, CSRF, the request files (contents, mode, one link, read by the real applier's spool code), names refused before any file is written with the same rule as `proxy_accounts.py`, protected accounts, confirmation, the state of each applier status, the one-time password (shown once, deleted, refused to another admin, never in the database, session, cache or logs), and incidents (updates, escaping, a maintenance notice next to failing probes). For the password reset: own account only (a supplied name is refused), the hourly limit, refusal while a request is pending, for disabled accounts, users without an account and service accounts, CSRF, and a password only its requester sees, once. For the Users page: the list read with the admin's token before the logout and never kept, paging, a failed, broken, repeating, too long or too slow read keeping the old list, no read for other users, authorization, the filters, search, and the request files of each action.

[`e2e/run.sh`](e2e/run.sh) runs the portal behind nginx (the repository's `nginx.conf` as it is, and a self-signed stand-in certificate) against the stubs on a network without a way out, checks the redirect and headers, posts incidents, and takes screenshots of every page, the admin console's included, in both languages at desktop and phone width with Playwright ([`e2e/shots.cjs`](e2e/shots.cjs)). [`e2e/status-refresh.cjs`](e2e/status-refresh.cjs) then checks the Status page's refresh with a test-controlled clock and a stubbed `/api/status/live`: the poll every 30 seconds, the age advancing between polls, Unknown and no proxy counts past 90 seconds while polls fail (also in Chinese, and also when the default proxy was Unknown from the start), the poll when the tab becomes visible, and the next poll after one that never gets an answer. [`e2e/status-bars.cjs`](e2e/status-bars.cjs) checks the status bars in both languages at desktop width and at phone widths of 390 and 360 pixels: no metric cards, 96 hourly bars per row first, the switch used with the keyboard changing every row and kept across a reload (and working when storage throws), the tooltip on focus and on hover, inside the window, also on a row the Tab key scrolls into view, the arrow keys, Home, Esc (also for a hovered bar) and one tab stop per list, the bars inside their row, the status chips inside their cells, the legend's two ends on one line, and no page overflow. [`e2e/self-reset.cjs`](e2e/self-reset.cjs) checks the password reset dialog in English at desktop width and in Chinese at phone width: focus inside it, Tab and Shift+Tab kept inside, Esc closing it and returning focus, the request file, the texts when the create or a poll gets an error page instead of an answer, the states as a stub applier status moves to applying and applied, the password shown once and copied, the result file deleted, and a second reveal refused. Last, with the stubs stopped, `status-refresh.cjs` checks that the note on missing monitoring data goes once a poll answers with monitoring data. `run.sh` needs ports 80 and 443 on `127.0.0.1`, and removes its containers and volume when done.

Translations: after changing a text, run `python ../manage.py makemessages -l zh_Hans --no-location --ignore "tests/*"` in `portal/`, translate the new entries in `portal/locale/zh_Hans/LC_MESSAGES/django.po` and remove any `fuzzy` flag. The image compiles them.
