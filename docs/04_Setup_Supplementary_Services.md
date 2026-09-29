# Setup Supplementary Services

## Contents

- [Setup Supplementary Services](#setup-supplementary-services)
  - [Contents](#contents)
  - [Introduction](#introduction)
  - [Proxy as a service](#proxy-as-a-service)
  - [SSL, HTTPS and reverse proxy](#ssl-https-and-reverse-proxy)
    - [Background knowledge](#background-knowledge)
    - [Create an SSL certificate](#create-an-ssl-certificate)
    - [Configure NGINX](#configure-nginx)
  - [All-in-one](#all-in-one)
  - [Harbor](#harbor)
    - [Install Harbor on the supplementary services node](#install-harbor-on-the-supplementary-services-node)
      - [Steps](#steps)
      - [Notes for NFS storage](#notes-for-nfs-storage)
      - [Provided configuration and patch](#provided-configuration-and-patch)
    - [Post-installation](#post-installation)

## Introduction

System Topology:

```text
┌───────────────────────────────────┐ ┌──────────────────────────────────┐
│             Login Node            │ │        NGINX Reverse Proxy       │
└─────────────┬─────────────────────┘ └────────┬────────┬────────────────┘
              │                                │        │
            Access      ┌────────Access────────┘      Access
              │         │                               │
┌─────────────▼─────────▼───────────┐ ┌─────────────────▼─────────────────┐
│     Determined AI GPU Cluster     │ │      Supplementary Services       │
├───────────────────────────────────┤ ├───────────────────────────────────┤
│                                   │ │                                   │
│ ┌──────┐ ┌────┐ ┌────┐ ┌────┐     │ │  ┌──────┐ ┌───────┐ ┌───────┐     │
│ │Master│ │GPU │ │GPU │ │GPU │     │ │  │      │ │       │ │       │     │
│ │      │ │    │ │    │ │    │ ... │ │  │Harbor│ │Grafana│ │ Other │ ... │
│ │ Node │ │Node│ │Node│ │Node│     │ │  │      │ │       │ │       │     │
│ └──────┘ └────┘ └────┘ └────┘     │ │  └──────┘ └───────┘ └───────┘     │
│                                   │ │                                   │
└───────────────────┬───────────────┘ └──────────┬────────────────────────┘
                    │                            │
                  Access                       Access
                    │                            │
┌───────────────────▼────────────────────────────▼────────────────────────┐
│                              TrueNAS - NFS                              │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│                              Storage Server                             │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

## Proxy as a service

The HTTP/SOCKS5 proxies of the cluster run on this VM as the `xray-*` services of the [all-in-one configuration](#all-in-one), each with an exporter for the Grafana v2ray dashboard. [Chapter 00](00_Network_Proxy.md) describes them, the default proxy (`xray-usca5-bwh-sla-1tb`, HTTP `59889`, SOCKS5 `59880`), how to add one with [`create_xray_service.py`](../services/xray/scripts/README.md), and how the login node and the other machines use them (Docker daemon, environment variables, proxychains, pip, git).

## SSL, HTTPS and reverse proxy

### Background knowledge

1) [What is SSL?](https://www.cloudflare.com/learning/ssl/what-is-ssl/)

2) [What is an SSL certificate?](https://www.cloudflare.com/learning/ssl/what-is-an-ssl-certificate/)

3) [What is HTTPS?](https://www.cloudflare.com/learning/ssl/what-is-https/)

4) [What is a reverse proxy?](https://www.cloudflare.com/learning/cdn/glossary/reverse-proxy/)

### Create an SSL certificate

The certificates will be stored in `/etc/ssl/private`.

1) `sudo apt install openssl`

2) `sudo su`

3) `cd /etc/ssl/private`

4) Create `CA.cnf`

    ```conf
    [req]
    distinguished_name  = req_distinguished_name
    x509_extensions     = root_ca
    prompt              = no

    [req_distinguished_name]
    C   = CN
    ST  = Zhejiang
    L   = Hangzhou
    O   = Westlake University
    OU  = SOE
    CN  = cvgl.lab

    [root_ca]
    basicConstraints    = critical, CA:true
    ```

5) Generate CA certificate

    ```bash
    # It is highly recommended to set a PEM pass prhase for CA
    openssl req -x509 -newkey rsa:2048 -out CA.cer -outform PEM -keyout CA.pvk -days 10000 -verbose -config CA.cnf -subj "/CN=cvgl Lab SOE Westlake University CA"
    ```

6) Create `Server.ext`

    ```conf
    extendedKeyUsage = serverAuth
    subjectAltName = @alt_names

    [alt_names]
    DNS.1 = cvgl.lab
    DNS.2 = *.cvgl.lab
    ```

7) Generate Server Certificate using CA

    ```bash
    # Generate the server's private key from request
    openssl req -newkey rsa:2048 -keyout Server.pvk -out Server.req -subj /CN=cvgl.lab

    # Sign the server's certificate using CA
    openssl x509 -req -CA CA.cer -CAkey CA.pvk -in Server.req -out Server.cer -days 10000 -extfile Server.ext -set_serial 0x1111

    # If private key has passphrase encryption, generate an unencrypted private key for NGINX
    openssl rsa -in Server.pvk -out Server-unsecure.pvk
    ```

### Configure NGINX

`Configurations` and `Dockerfile` can be found [here](../services/nginx/).

You can add a temporary `docker-compose.yaml` in the `nginx` folder to test the configurations:

```yaml
services:
  reverseproxy:
    build: ./build
    image: reverseproxy
    ports:
        - 80:80
        - 443:443
    restart: always
    volumes:
      - ./data/html:/usr/share/nginx/html:ro
      - /etc/ssl/private:/opt/ssl:ro
```

Then run the following command to test it:

```bash
docker compose up
```

You can add the `CA.cer` created above to your browser (or the whole system) to depress the warning:

- [Tutorial by Thomas Leister](https://thomas-leister.de/en/how-to-import-ca-root-certificate/)

- [Tutorial from VMware (Windows) - Add a Root Certificate in Google Chrome](https://docs.vmware.com/en/VMware-Adapter-for-SAP-Landscape-Management/2.1.0/Installation-and-Administration-Guide-for-VLA-Administrators/GUID-D60F08AD-6E54-4959-A272-458D08B8B038.html)

- [Tutorial from Ubuntu - Installing a root CA certificate](https://ubuntu.com/server/docs/security-trust-store)

Finally, add the corresponding HOSTS to your PC:

```text
10.0.1.68 cvgl.lab
10.0.1.68 gpu.cvgl.lab
10.0.1.68 grafana.cvgl.lab
10.0.1.68 harbor.cvgl.lab
```

Open the URLs in your browser:

![NGINX running](images/04_NGINX.png)

Note: You can copy the `CA.cer` to NGINX data for occasional downloads:

```bash
sudo cp /etc/ssl/private/CA.cer services/nginx/data/html/cvgl.crt   # in the repository root
```

This will be useful in the [following section](#harbor).

## All-in-one

We have constructed an all-in-one [docker-compose file](../services/docker-compose.yml) to launch most supplementary services mentioned above except `Harbor` the container registry which will be discussed in the next section.

More details can be found in the [README of services](../services/README.md).

## Harbor

In this section, we will discuss how to install and configure Harbor in our cluster.

### Install Harbor on the supplementary services node

#### Steps

This is a typical Harbor installation showcase:

- First download Harbor's [installer](https://github.com/goharbor/harbor/releases)
- Edit `harbor.yml`, update `hostname`, `http.port`, `external_url`, `data_volume`, `log.location`
- Run `sudo install.sh`
- Run `docker compose down`
- Edit `docker-compose.yml`, update PostgreSQL database volume path
- Run `docker compose up -d`

However, in our production environment, we need to deploy Harbor's storage on NFS. We will discuss the details in the following two sections:

#### Notes for NFS storage

- Move PostgreSQL's `database` folder outside of `data` to set separate ACL
- Set ACL `10000:10000` for `data` & `999:999` for `database`
- In NFS share configuration, enable map-root
- Use NFSv3 for the `database` (to avoid stale file handle)

Edit ACL for data:
![Edit ACL for data](./images/04_Harbor_edit-acl-data.png)

Edit ACL for database:
![Edit ACL for database](./images/04_Harbor_edit-acl-database.png)

Create NFS share for data:
![Create NFS share for data](./images/04_Harbor_nfs-data.png)

Create NFS share for database:
![Create NFS share for database](./images/04_Harbor_nfs-database.png)

Set up both NFSv4 and v3 compatablity:
![Set up both NFSv4 and v3 compatablity](./images/04_Harbor_nfsv4.png)

Example of `/etc/fstab` (the current entries are in the [reference fstab](../services/system-configurations/etc/fstab)):

```text
nas.cvgl.lab:/mnt/Peter/SupplementaryServices/harbor/data       /srv/nfs/var/harbor/data        nfs vers=3,defaults,async,noatime,hard,rsize=1048576,wsize=1048576,_netdev 0 2
nas.cvgl.lab:/mnt/Peter/SupplementaryServices/harbor/database   /srv/nfs/var/harbor/database    nfs vers=3,defaults,async,noatime,hard,rsize=1048576,wsize=1048576,_netdev 0 2
```

#### Provided configuration and patch

You can use the provided [`harbor.yml`](../services/harbor/harbor.yml) to install [Harbor](https://github.com/goharbor/harbor/releases/tag/v2.15.2) and switch to NFS by replacing `/srv/nfs/var/harbor/data/database` to `/srv/nfs/var/harbor/database` :

```bash
cd <project_root>/services/harbor
tar -xvzf /path/to/harbor-offline-installer-v2.15.2.tgz    # current version (harbor.yml has _version: 2.15.0)
mv harbor installer && cd installer
cp ../harbor.yml .
sudo bash ./install.sh
sudo docker compose down
sudo sed -i 's/\/srv\/nfs\/var\/harbor\/data\/database/\/srv\/nfs\/var\/harbor\/database/g' docker-compose.yml
sudo docker compose up -d
```

Current settings:

```yml
hostname: harbor.cvgl.lab
external_url: https://harbor.cvgl.lab
database.password: <secrect>
data_volume: /srv/nfs/var/harbor/data
log.location: /srv/nfs/var/harbor/data/log
```

### Post-installation

1) Configure HOSTS on each node. Make sure these lines exist (as in the [reference hosts file](../services/system-configurations/etc/hosts)):

    ```text
    192.168.233.8 cvgl.lab
    192.168.233.8 harbor.cvgl.lab
    ```

2) Trust the CA certificate on each node:

    ```bash
    sudo mkdir -p /etc/docker/certs.d/harbor.cvgl.lab
    cd /etc/docker/certs.d/harbor.cvgl.lab
    sudo wget https://cvgl.lab/cvgl.crt --no-check-certificate
    ```

3) Update the NGINX upstream (in [`nginx.conf`](../services/nginx/build/nginx.conf))

    ```nginx
    upstream harbor {
        server 192.168.233.8:50000;
    }
    ```

4) Rebuild and restart NGINX (in the `services` folder of the all-in-one configuration, where the service is called `nginx`)

    ```bash
    docker compose build nginx
    docker compose up -d --force-recreate --no-deps nginx
    ```

5) Log in with the URL `https://harbor.cvgl.lab`. Change the default password.

    ![Harbor](./images/04_Harbor.png)

Now the system admin can manage users and projects through the web dashboard.

To test the Harbor registry:

```bash
docker login harbor.cvgl.lab # You only need to login once
docker pull hello-world
docker tag hello-world harbor.cvgl.lab/library/hello-world
docker push harbor.cvgl.lab/library/hello-world
```

The outputs should look like this:

```text
Using default tag: latest
The push refers to repository [harbor.cvgl.lab/library/hello-world]
e07ee1baac5f: Pushed 
latest: digest: sha256:f54a58bc1aac5ea1a25d796ae155dc228b3f0e11d046ae276b39c4bf2f13d8c4 size: 525
```

Note: to restart the Harbor services, go to the installation folder and use `docker compose` commands:

```bash
sudo docker compose up -d --force-recreate --remove-orphans
```
