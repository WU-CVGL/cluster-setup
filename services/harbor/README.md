# Harbor

## Steps

- First download harbor installer
- Edit harbor.yaml, update `hostname`, `http.port`, `external_url`, `data_volume`, `log.location`
- Run `sudo install.sh`
- Run `docker-compose down`
- Edit `docker-compose.yml`, update postgresql database volume path
- Run `docker-compose up -d`

## Notes

When using NFS storage: 

- Move postgresql `database` outside of `data` to set seperate ACL
- Set ACL `10000:10000` for `data` & `999:999` for `database`
- Use NFSv3 for database (to avoid stale file handle)