# Harbor

## Steps

- First download harbor installer
- Edit harbor.yaml, update `hostname`, `http.port`, `external_url`, `data_volume`, `log.location`
- Run `sudo install.sh`
- Run `docker-compose down`
- Edit `docker-compose.yml`, update postgresql database volume path
- Run `docker-compose up -d`