# Proxy account applier

`proxy_apply.py` applies proxy account changes requested by the proxy portal: it updates the accounts file (`services/proxy-accounts/accounts.json`), renders and checks the configs, restarts the proxies and verifies the result. It runs on the supplementary services VM as root, started by systemd. The portal only writes request files into a spool folder; it never gets the Docker socket, the accounts file or a shell on the host. Python 3.8 or later, standard library only. How accounts work: [docs/00](../../../docs/00_Network_Proxy.md#accounts).

## Install

The units and the settings file are in [`services/system-configurations/etc`](../../system-configurations/etc): `systemd/system/proxy-apply.service`, `proxy-apply.path`, `proxy-apply.timer` and `default/proxy-apply`.

```sh
cd ~/ws/cluster-setup/services
sudo useradd --system --uid 3089 --user-group --no-create-home --shell /usr/sbin/nologin proxy-portal   # the portal's image runs as 3089
sudo install -m 644 system-configurations/etc/systemd/system/proxy-apply.{service,path,timer} /etc/systemd/system/
sudo install -m 644 system-configurations/etc/default/proxy-apply /etc/default/proxy-apply
sudoedit /etc/default/proxy-apply   # PROXY_APPLY_SERVICES_DIR: this checkout's services folder
sudo python3 proxy-portal/applier/proxy_apply.py --requester proxy-portal init   # the spool
sudo systemctl daemon-reload
sudo systemctl enable --now proxy-apply.path proxy-apply.timer
```

The service runs the scripts of this checkout as root: keep the checkout writable only by the admins.

The settings come from `/etc/default/proxy-apply` only, never from a request:

| Setting | Default | |
| :--- | :--- | :--- |
| `PROXY_APPLY_SERVICES_DIR` | the folder above this one | the `services/` folder of the checkout |
| `PROXY_APPLY_REQUESTER` | | user name or uid allowed to write requests |
| `PROXY_APPLY_SPOOL` | `/var/lib/proxy-portal/spool` | also fixed in `proxy-apply.path` |
| `PROXY_APPLY_COMPOSE_FILE` | `<services>/docker-compose.yml` | |
| `PROXY_APPLY_COMPOSE_PROJECT` | Compose's default | |
| `PROXY_APPLY_HA_HTTP` | `127.0.0.1:30889` | `ha-general`'s HTTP port on the host |
| `PROXY_APPLY_HA_PRIVATE_HTTP` | `127.0.0.1:31889` | `ha-private`'s HTTP port on the host |
| `PROXY_APPLY_PROBE_URL` | `https://www.google.com/generate_204` | fetched through the HA entries to verify |
| `PROXY_APPLY_VERIFY_TIMEOUT` | `60` | seconds |

`proxy-apply.path` starts the service while a request is waiting; `proxy-apply.timer` runs it every 5 minutes to expire results and to recover an interrupted apply. The service has no start limit, so a burst of quick runs does not stop the path unit; a failed run waits 30 seconds before it ends, so a request it cannot take is retried at that pace. It exits 1 when a rollback did not finish, and 2 when it cannot run (spool, accounts file, requester) or cannot remove a request entry; refused and rolled-back requests are reported in their status files. While a run cannot proceed, stop `proxy-apply.path` (`sudo systemctl stop proxy-apply.path`) until it is fixed. Its log is in `journalctl -u proxy-apply`. Files the applier replaces keep their owner, so the admins' `proxy_accounts.py` keeps working on them.

## Spool

| Path | Owner, mode | Content |
| :--- | :--- | :--- |
| `requests/` | requester, `0700` | requests, one file each |
| `results/` | requester, `0700` | one-time results: a new password, file mode `0400`, owned by the requester |
| `status/` | root, `0755` | one status file per request |
| `status.json` | root, `0644` | the accounts without passwords: `proxy_accounts.py status --json` |
| `sequence.json` | root, `0644` | the last apply sequence number (`applied_seq`) |

The portal's container mounts the spool and runs as the requester's uid, 3089, fixed in its image ([`../README.md`](../README.md)); files written under any other uid are refused. Check that 3089 is free (`getent passwd 3089`) before creating the user. It writes a request under a name that does not end in `.json` and then renames it to `<id>.json`, so a request is never read half-written. The applier reads every `*.json` entry and removes it, whether valid or not. It opens the entry without following links, and accepts only a regular file with one link, owned by the requester and at most 4096 bytes.

## Requests

```json
{"id": "0f8e1c2a-1111-4222-8333-944455556666", "op": "disable", "username": "alice"}
```

- **`id`**: 8 to 64 characters, lowercase letters, digits and `-` (a UUID), and the file name without `.json`. An id that already has a status file is dropped. Status files are kept, so an id is applied at most once; the portal must use a new id for every request.
- **`op`**: `add`, `rotate`, `enable`, `disable`, `remove`, `admin-on` or `admin-off`. An admin may use `ha-private` and the private proxies ([docs/00](../../../docs/00_Network_Proxy.md#private-entries)); a service account cannot be one (`failed`, stage `request`). Raw auth, rendering and the classification of the proxies stay with `proxy_accounts.py` and `xray/public-proxies.txt`.
- **`username`**: the account, checked by the same validator as `proxy_accounts.py`. `svc-ha` and `svc-monitor` are refused: they are changed with `proxy_accounts.py` only.
- No other key is accepted. A request carries no password, path or command.

**Passwords** for `add` and `rotate` are generated by the applier, as `proxy_accounts.py` does. The new password is written to `results/<id>.json` (`{"id", "username", "password"}`) once the change is live (`applied` or `unconfirmed`). The file is mode `0400` and owned by the uid that wrote the request. The portal shows it once and deletes it; the applier deletes results older than 10 minutes. So no secret travels from the portal to the host, requests, statuses and logs never hold a password, a failed request leaves none behind, and a password is handed out only once its change is live. `result: false` in a status means no result is waiting: it could not be written, or a later request of the same run replaced that password (`superseded_by` names it).

## Statuses

`status/<id>.json` holds `id`, `status` and `updated_at`, `op` and `username` once the request could be read, plus, for `applied` and `unconfirmed`, `applied_seq`: the order in which the applier made the batch live, increasing across runs. Requests for one account are never in the same batch, so `applied_seq` orders them as they took effect, whatever their file times or the portal's records say.

| `status` | Meaning |
| :--- | :--- |
| `applying` | taken, in the current batch |
| `applied` | live and verified; `restarted` lists the services restarted, `changed: false` when nothing had to change |
| `failed` | not applied; `stage` and `error` say why. `restored: true`: the previous files and running state are back (or nothing was touched). `restored: false`: the rollback did not finish. If the files could not be restored, `proxy-accounts/.apply-backup/` stays, and every run retries it first and applies nothing else until it succeeds (exit 2). Check `journalctl -u proxy-apply` and `docker compose ps xray-ha`, fix the cause, then `sudo systemctl start proxy-apply`. To restore by hand, `manifest.json` in that folder lists each file and its backup |
| `unconfirmed` | live, and every login that must be refused is refused, but the logins that must pass got no 204 within the verify timeout (all proxies or the probe URL may be down). The result file is written |

`stage` is one of `request` (invalid, or the account does not allow the operation, e.g. `admin-on` for a service account; the other requests of the batch go on), `accounts`, `render`, `test`, `precheck`, `backup`, `swap`, `restart`, `verify`, `commit` or `interrupted`.

## How a batch is applied

The applier holds the accounts lock (`proxy-accounts/.lock`, the one `proxy_accounts.py` takes) for the whole run, and takes up to 64 pending requests at a time, oldest first. A batch ends before a second request for the same account, which goes into the next batch, so every request is verified on its own:

1. Applies the requests to a copy of the accounts in memory; one that cannot apply (e.g. `add` of an existing account) fails alone.
2. Renders every config in memory, refusing an HA example in which `ha-general` could reach a private proxy. Disabled accounts are left out of every inbound; non-admins out of `ha-private` and the private proxies.
3. Decides what counts as changed. When `proxy-accounts/applied.json` records the accounts file and the rendered files as they are on disk, the running services hold them, and only the files this batch changes count. A batch that changes nothing then checks each request's own account on both HA entries, as in step 7, and ends there (`applied`, `changed: false`, nothing restarted; `unconfirmed` when only the 204s are missing); a wrong answer, or no 407, sends it on as below. Otherwise, after a change made with `proxy_accounts.py` or without `applied.json`, every rendered file counts as changed (the private proxies' configs included): the batch is applied, restarted and verified like any other. `proxy_accounts.py render` removes `applied.json` whenever it writes a file, since a restart by hand is not recorded; so do a rollback that did not finish and the recovery of an apply killed after its commit.
4. Runs `xray run -test` on each changed Xray config, with the image `xray-ha` is pinned to in the Compose file (digest required), the config on stdin and no network.
5. Checks that `xray-ha` runs, and that every Xray service it will restart is configured with that same image and runs a container created from it (`precheck`). A restart keeps a container's image, and older Xray versions ignore the inbounds' `users`, so a proxy on one would accept anyone: recreate it with `docker compose up -d <service>`. Then it keeps the files it is about to replace in `proxy-accounts/.apply-backup/`, and replaces them atomically: the configs, then the accounts file.
6. Restarts a fixed set of Compose services: `xray-ha`; with raw auth on, every running proxy; a proxy whose config changed, which for a private proxy is every change to the admins it accepts (`admin-on`, `admin-off`, and `rotate`, `disable`, `enable` or `remove` of an admin), and is every running proxy, also with raw auth off, when every rendered file counts as changed (step 3), since a proxy may still run with accounts; `blackbox-exporter` only if its config changed, which a request cannot cause (or a change made with `proxy_accounts.py`, as in step 3).
7. Verifies through the HTTP ports of `ha-general` and `ha-private`, fetching the probe URL. On each entry, `svc-monitor` and every login the entry newly accepts (added, enabled, rotated, and on `ha-private` an admin turned on) must get 204, while every login it no longer accepts (disabled, removed, a replaced password, and on `ha-private` an admin turned off) and a wrong password must get 407. Each request's own account is checked on both entries in the state it asks for, also when the batch found it already in that state. `ha-private` must also refuse with 407 every non-admin the batch changed, and one it did not. A running entry answers 407 whatever the proxies do. The private proxies' own entries are not probed: their configs are rendered from the same accounts as `ha-private`.
8. On a wrong answer, or no 407 within the timeout, it restores the files, restarts the same services and checks `svc-monitor` on both entries again (`failed`). When only the 204s are missing, the change stays (`unconfirmed`).

Then it marks the backup committed, writes `proxy-accounts/applied.json`, the results and the statuses, drops the backup and writes `status.json`. `applied.json` holds the hashes of the accounts file and of the rendered files at the applier's last apply; `status` compares it with the current file (`unapplied_changes`). After a change with `proxy_accounts.py` it is `true`, and after a `render` that wrote a file it is `null` (unknown) until the next apply. If the applier is killed after taking a batch and before the commit, the next run restores the files from the backup, if any were replaced, and marks the batch `failed` (`interrupted`). If it is killed after the commit, the next run finishes the batch instead: it writes the remaining statuses, removes `applied.json`, restarts nothing, and reports a result not yet written as `result: false`. Passwords are never logged or written to a status: errors are scrubbed of every known password.

## Restart impact

Every change restarts the HA entry, a change to the admins also the private proxies, and with raw auth on every proxy, as does an apply after a change made with `proxy_accounts.py` (step 3). Established connections through them drop: downloads break and clients have to retry. Requests that start during the restart fail for a few seconds, as when `xray-ha` starts ([the HA entry](../../../docs/00_Network_Proxy.md#the-ha-entry)). A rollback restarts them once more, and a failed apply can keep the entry down for up to the verify timeout. Requests that arrive together are applied with one restart; each further request for the same account adds one. A request that changes nothing (enabling an enabled account) restarts nothing, unless the running services may not hold the files or its account gets the wrong answer (step 3).

While raw auth is off, disabling an account stops it only at the HA entries (and, for an admin, the private proxies): the public proxies' own entries require no account, so it can still use them.

## Tests

```sh
cd services/proxy-portal/applier && python3 -B -m unittest discover -s tests -v
```

They cover request validation, changes made with `proxy_accounts.py` and not restarted (also after a restart by hand), the image check, the apply sequence, the spool's handling of links, folders, foreign owners and oversized files, requests that cannot be read or removed, several requests for one account, `admin-on` and `admin-off` with the private proxy's restart, and the apply steps with injected failures: render, `xray -test`, restart, verification (including an `ha-private` that lets a non-admin in, or refuses a new admin), rollback, and an apply interrupted before and after the commit. Docker and the HA entries are simulated.
