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

## Introduction

We are currently offering these web services:

- Homepage
  - `https://cvgl.lab`
- GPU cluster dashboard
  - `https://gpu.cvgl.lab`
- Harbor
  - `https://harbor.cvgl.lab`
- Grafana
  - `https://grafana.cvgl.lab`

- Background services:
  - prometheus
  - node-exporter
  - v2ray-exporter

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

In the [previous section](./01_First-time_Setup_of_Cluster_Nodes.md#setup-a-temporary-proxy-service), we used a temporary proxy service. In this section, we will set up a production-ready proxy service via docker-compose, along with a monitoring endpoint for a Grafana dashboard.

Here is an example, whose full version can be found in [all-in-one configuration](#all-in-one):

```yaml
version: '3'

networks:
  grafana_monitor:
    driver: bridge

services:
  xray-jp-central:
    image: teddysun/xray
    restart: unless-stopped
    networks:
      - grafana_monitor
    environment:
      TZ: Asia/Shanghai
    ports:
      - 10089:1089
      - 18889:8889
    volumes: 
      - ./xray/jp-central/config:/etc/xray
      - ./xray/jp-central/log:/var/log/xray
    expose:
      - 10085

  xray-jp-central-exporter:
    image: wi1dcard/v2ray-exporter:master
    networks:
      - grafana_monitor
    environment:
      TZ: Asia/Shanghai
    restart: unless-stopped
    command: 'v2ray-exporter --v2ray-endpoint "xray-jp-central:10085" --listen ":9550"'
    expose:
      - 9550
```

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

`Configurations` and `Dockerfile` can be found [here](https://git.cvgl.lab/Cluster_User_Group/CVGL-Services/src/branch/nginx/nginx/).

You can add a temporary `docker-compose.yaml` in the `nginx` folder to test the configurations:

```yaml
version: '3'

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
docker-compose up
```

You can add the `CA.cer` created above to your browser (or the whole system) to depress the warning:

- [Tutorial by Thomas Leister](https://thomas-leister.de/en/how-to-import-ca-root-certificate/)

- [Tutorial from VMware (Windows) - Add a Root Certificate in Google Chrome](https://docs.vmware.com/en/VMware-Adapter-for-SAP-Landscape-Management/2.1.0/Installation-and-Administration-Guide-for-VLA-Administrators/GUID-D60F08AD-6E54-4959-A272-458D08B8B038.html)

- [Tutorial from Ubuntu - Installing a root CA certificate](https://ubuntu.com/server/docs/security-trust-store)

Finally add the corresponding HOSTS to your PC:

```text
10.0.2.162 cvgl.lab
10.0.2.162 gpu.cvgl.lab
10.0.2.162 grafana.cvgl.lab
10.0.2.162 harbor.cvgl.lab
```

Open the URLs in your browser:

![NGINX running](images/02_NGINX.png)

Note: You can copy the `CA.cer` to NGINX data for occasional downloads:

```bash
sudo cp /etc/ssl/private/CA.cer CVGL-Services/nginx/data/html/cvgl.crt
```

This will be useful in the [following section](#harbor).

## All-in-one

(TODO)

We have constructed an all-in-one [docker-compose file](https://git.cvgl.lab/Cluster_User_Group/CVGL-Services/src/branch/nginx/docker-compose.yaml) to launch most supplementary services mentioned above except `Harbor` the container registry which will be discussed in the next section.

To launch the all-in-one services, simply run the command in the `CVGL-Services` folder:

```bash
docker-compose up -d
```

To rebuild one service, for example, the NGINX reverse proxy, run

```bash
docker-compose build reverseproxy
```

To force recreate the services (when changed some configurations), run

```bash
docker-compose up -d --force-recreate --remove-orphans
```

## Harbor

 In this section, we will discuss how to install and configure Harbor in our cluster.

1) Install Harbor. You can start from the official docs [here](https://goharbor.io/docs/2.6.0/install-config/configure-yml-file/). Also, check out the notes [here](https://git.cvgl.lab/Cluster_User_Group/CVGL-Services/src/branch/nginx/harbor/README.md) to customize.

2) Configure HOSTS on each node. Add the following lines:

    ```text
    192.168.123.162 cvgl.lab
    192.168.123.162 harbor.cvgl.lab
    ```

3) Trust the CA certificate on each node:

    ```bash
    sudo mkdir -p /etc/docker/certs.d/harbor.cvgl.lab
    cd /etc/docker/certs.d/harbor.cvgl.lab
    sudo wget https://cvgl.lab/cvgl.crt --no-check-certificate
    ```

4) Update the NGINX upstream

    ```nginx
    upstream harbor {
        server 192.168.123.164:50000;
    }
    ```

5) Rebuild and restart NGINX

    ```bash
    docker-compose build reverseproxy
    docker-compose up -d --force-recreate --no-deps reverseproxy
    ```

6) Log in with the URL `https://harbor.cvgl.lab`. Change the default password.

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

Note: to restart the Harbor services, go to the installation folder and use `docker-compose` commands:

```bash
sudo docker-compose up -d --force-recreate --remove-orphans
```
