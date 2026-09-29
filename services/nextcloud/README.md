# Nextcloud

## Envs

Both files are gitignored and only exist on the supplementary services VM; `docker compose` refuses to start if one is missing.

### nextcloud.env<sup>1,4</sup>
```
NEXTCLOUD_TRUSTED_DOMAINS=pan.cvgl.lab
TRUSTED_PROXIES=<Your nextcloud subnet CIDR>
```

### db.env<sup>2,3</sup>
```
POSTGRES_DB=nextcloud
POSTGRES_USER=nextcloud
POSTGRES_PASSWORD=<Your Password>
```

## Notes
0. When using NFS storage: use NFSv4 && enable NFS maproot && set dataset permission to 82:82
1. CIDR: the subnet of the `nextcloud` Docker network, e.g. `docker network inspect services_nextcloud --format '{{(index .IPAM.Config 0).Subnet}}'` (Docker takes it from the `default-address-pools` in `/etc/docker/daemon.json`, so an address like 172.22.0.5/16 is not it)
2. Change file permission of `db.env` for security: `chmod 600 db.env`
3. Keep `db.env` owned by the admin user that runs `docker compose` (cvgladmin, uid 1000), mode 600. Compose reads every service's env files as the invoking user, so a root-owned 0600 `db.env` makes `docker compose up`, `build` and `config` fail for ALL services unless run with sudo.
4. These are the variables the official `nextcloud` image reads (it ignores `NEXTCLOUD_DOMAIN_NAME` and `NEXTCLOUD_TRUSTED_DOMAIN`). `NEXTCLOUD_TRUSTED_DOMAINS` is only used by the first installation; afterwards the trusted domains live in Nextcloud's `config.php`. `TRUSTED_PROXIES` and the `OVERWRITE*` variables (`OVERWRITEPROTOCOL`, `OVERWRITEHOST`, `OVERWRITECLIURL`) apply whenever `nextcloud-app` is recreated. The NGINX reverse proxy terminates TLS for https://pan.cvgl.lab and does not send `X-Forwarded-Proto`; if Nextcloud generates http:// links, check `overwriteprotocol` in `config.php` and, if it is not set, set `OVERWRITEPROTOCOL=https` (this also affects access through the direct port 8008).
