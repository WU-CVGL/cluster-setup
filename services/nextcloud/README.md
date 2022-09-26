# Nextcloud envs

## nextcloud.env<sup>1</sup>
```
NEXTCLOUD_DOMAIN_NAME=<Your Host Name>
NEXTCLOUD_TRUSTED_DOMAIN=<Your Host Name>
TRUSTED_PROXIES=<Your nextcloud subnet CIDR>
```

## db.env<sup>2,3</sup>
```
POSTGRES_DB=nextcloud
POSTGRES_USER=nextcloud
POSTGRES_PASSWORD=<Your Password>
```

## Notes
1. CIDR example: 172.22.0.5/16
2. Change file permission of `db.env` for security: `chmod 600 db.env`
3. Change owner of `db.env` to root: `sudo chown root:root db.env`