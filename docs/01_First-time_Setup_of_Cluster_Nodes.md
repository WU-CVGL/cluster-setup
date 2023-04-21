# First-time setup of Cluster Nodes

## Contents

- [First-time setup of Cluster Nodes](#first-time-setup-of-cluster-nodes)
  - [Contents](#contents)
- [System Topology](#system-topology)
- [Set Up the Storage \& Management Server](#set-up-the-storage--management-server)
  - [Install VMware ESXi](#install-vmware-esxi)
  - [Configure VMware ESXi](#configure-vmware-esxi)
    - [Configure PCI passthrough](#configure-pci-passthrough)
    - [Configure networks](#configure-networks)
  - [Install VMs](#install-vms)
  - [Configure TrueNAS](#configure-truenas)
    - [Configure storage Pools](#configure-storage-pools)
    - [Add public datasets](#add-public-datasets)
    - [Configure NFS share for public datasets](#configure-nfs-share-for-public-datasets)
    - [Configure cron jobs](#configure-cron-jobs)
      - [Scrubbing](#scrubbing)
      - [S.M.A.R.T. test](#smart-test)
      - [Snapshots](#snapshots)
      - [Replication](#replication)
- [Set Up the GPU Nodes (Agents)](#set-up-the-gpu-nodes-agents)
  - [Notes on Ubuntu](#notes-on-ubuntu)
    - [Disable unattended-updates](#disable-unattended-updates)
    - [Disable GUI](#disable-gui)
    - [Disable network\*-wait-online](#disable-network-wait-online)
    - [SSH Keep-Alive](#ssh-keep-alive)
    - [Only 4/8 GPUs show up in nvidia-smi](#only-48-gpus-show-up-in-nvidia-smi)
    - [In case you forgot Server BMC (IPMI) password](#in-case-you-forgot-server-bmc-ipmi-password)
    - [Prevent Docker and VPN IP address conflicts](#prevent-docker-and-vpn-ip-address-conflicts)
    - [Security Related Rules](#security-related-rules)
  - [Setup Open Source Mirrors \& nvidia-docker](#setup-open-source-mirrors--nvidia-docker)
    - [APT](#apt)
    - [Python PyPI (pip)](#python-pypi-pip)
  - [Install Docker-CE](#install-docker-ce)
  - [Install Nvidia-docker](#install-nvidia-docker)
  - [Set up hosts](#set-up-hosts)
  - [Configure a Proxy for Docker](#configure-a-proxy-for-docker)
    - [(Deprecated) Setup a temporary proxy service](#deprecated-setup-a-temporary-proxy-service)
    - [Verify the proxy service](#verify-the-proxy-service)
    - [Configure `Docker` to use the proxy](#configure-docker-to-use-the-proxy)
  - [Install Determined AI Systemwide](#install-determined-ai-systemwide)
    - [Pypi cryptography \& pyOpenSSL dependency conflict](#pypi-cryptography--pyopenssl-dependency-conflict)
  - [Do stress test](#do-stress-test)
  - [Maintainance: Upgrade APT packages \& `Determined AI`](#maintainance-upgrade-apt-packages--determined-ai)
  - [Common References](#common-references)
    - [Cluster Management System](#cluster-management-system)
    - [System \& Design Reference](#system--design-reference)
    - [Wikis / DBs / KBs](#wikis--dbs--kbs)
    - [Benchmarks](#benchmarks)
    - [Forums / Q\&As](#forums--qas)
    - [Media](#media)

# System Topology

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

GPU Nodes: bare metal servers running Ubuntu;

Storage & Management Server: running VMware ESXi, holding 4 VMs:

- Core Services VM, running Determined AI Master Node;
- Supplementary Services VM, holding supplementary services;
- Login Node VM, the login node, which works as the entry point of the cluster;
- TrueNAS VM, provides ZFS file storage and NFS sharing service.

# Set Up the Storage & Management Server

## Install VMware ESXi

[Reference](https://docs.vmware.com/en/VMware-vSphere/7.0/com.vmware.esxi.install.doc/GUID-B2F01BF5-078A-4C7E-B505-5DFFED0B8C38.html)

## Configure VMware ESXi

### Configure PCI passthrough

Configure PCI passthrough for SAS controllers, PCIe (NVMe) SSDs and a 10GbE port.
All of these will be used in the TrueNAS VM.

![Passthru](./images/01_ESXI_PASSTHRU.png)

### Configure networks

Create a vSwitch for 10GbE LAN:
![VSwitch](./images/01_ESXI_INIT.png)

Create a port group for 10GbE LAN:
![PG](./images/01_ESXI_INIT_02.png)

Configure VMs:

![Core node](./images/01_ESXI_INIT_03.png)

![TrueNas configuration](./images/01_ESXI_INIT_04.png)

Set up reservations for critical workloads:

![Core service reservation](./images/01_ESXI_INIT_05.png)

![TrueNAS reservation](./images/01_ESXI_INIT_06.png)

## Install VMs

 ![Init](images/01_ESXI.png)

 For Ubuntu, uncheck the box *Set up this disk as an LVM group*.

 ![noLVM](images/01_ESXI_02.png)

 Configure Networks:

 ![Network](images/01_ESXI_03.png)

 Set up hostnames and usernames then continue:

 ![Names](images/01_ESXI_04.png)

 Post-installation:

 Add the IP of the core service VM to the login node's `/etc/environment`:

 ```bash
 DET_MASTER="10.0.1.66"
 ```

## Configure TrueNAS

Here is an example of configuring a typical TrueNAS storage for lab use.

This example shows how to create a RAID-Z2 HDD pool for warm storage and a JBOD PCIe SSD pool for hot storage, as well as cron job configurations: data scrubbing, S.M.A.R.T. testing, snapshots, and replication.

### Configure storage Pools

Create the SSD pool:

![Create SSD Pool](./images/01_NAS.png)

The SSD pool is a JBOD (stripe) pool.

Create the HDD pool:

![Create HDD Pool](./images/01_NAS_02.png)

The HDD pool is a RAID-Z2 pool with six-18T-disk data-VDev(s) and two mirrored SATA SSDs mirrored as one metadata-VDev.

### Add public datasets

 Go to Storage -> Pools, and create a dataset under either pool `HDD` or `SSD`:

 ![Create dataset](./images/01_NAS_03.png)

 Then edit the permission of the dataset.
 To make it public, set `User` and `Group` to `nobody` and `nogroup`.
 Remember to click the checkboxes of  **Apply User** and **Apply Group**.
 Also, click the checkboxes of **Write** and **Execute** **Access** on the right.

 ![Public dataset settings on TrueNAS](./images/01_NAS_04.png)

### Configure NFS share for public datasets

First, enable NFS service:

![Enable NFS](./images/01_NAS_05.png)

Go to `Sharing/NFS/Add`, and select the dataset just created above.
In **Networks**, let `Authorized Networks = 10.0.1.64/27,192.168.233.0/24`.
Click **SUBMIT** at the bottom of the page.

![Public dataset NFS Share](./images/01_NAS_06.png)

### Configure cron jobs

Cron jobs are scheduled at recurring intervals, specified using a format based on [unix-cron](https://crontab.cronhub.io/). You can define a schedule so that your job runs multiple times.

#### Scrubbing

The scrubbing is auto-configured by the TrueNAS system.

![Scrubbing](images/01_NAS_CRON.png)

#### S.M.A.R.T. test

The S.M.A.R.T. test is configured to do a **SHORT** test at 00:00 every Sunday
and to do a **LONG** test at 03:00 on the first day of every month.

![SMART](images/01_NAS_CRON_02.png)

#### Snapshots

Add a periodic snapshot task for the `HDD` dataset.
The `HDD/labdata0` will be used for replication thus excluded.

![HDD snapshot](./images/01_NAS_CRON_03.png)

Add a periodic snapshot task for the `SSD/labdata0` sub-dataset.
This task will be used to auto-trigger the replication.

![SSD snapshot](./images/01_NAS_CRON_04.png)

The snapshots are configured to execute at 05:00 every day.

The snapshots can be found here:

![SSD snapshots](./images/01_NAS_CRON_05.png)

You can use these snapshots to export or rollback the accidentally deleted files.

#### Replication

![Replication from SSD to HDD](./images/01_NAS_CRON_06.png)

The Replication from the unprotected SSD pool to the RAID-Z2 HDD pool is set
to synchronize with the periodic snapshot task of `SSD/labdata0`.

After a successful replication, the report and logs can be seen here:

![Replication successful](./images/01_NAS_CRON_07.png)

References:

> https://www.truenas.com/docs/core/coretutorials/tasks/creatingreplicationtasks/localreplication/
>
> https://www.truenas.com/docs/core/coretutorials/tasks/creatingreplicationtasks/advancedreplication/

# Set Up the GPU Nodes (Agents)

## Notes on Ubuntu

### Disable unattended-updates

This prevents Ubuntu from applying kernel live patches from Canonical, which causes the `nvidia-driver-dkms` update and thus NVIDIA GPU driver to fail.

```bash
sudo apt purge unattended-upgrades
```

Keep an eye on it - try to avoid accidentally installing it

### Disable GUI

This prevents automatic hibernation caused by desktop environments installed (sometimes for enabling remote desktops, etc.) on the server.

```bash
sudo systemctl set-default multi-user
```

### Disable network*-wait-online

This prevents the system from getting stuck during startup when some ethernet port has no internet access - which is common in private networks.

```bash
sudo systemctl disable NetworkManager-wait-online.service
sudo systemctl disable systemd-networkd-wait-online.service
```

Another way is to add `optional=true` in the `netplan` configuration on respective ethernet ports.
For example, in `/etc/netplan/00-installer-config.yaml`

```yaml
network:
  ethernets:
    ens114f0:
      addresses:
      - 192.168.233.10/24
      optional: true
```

### SSH Keep-Alive

Change these values in `/etc/ssh/sshd_config`:

```text
TCPKeepAlive yes
ClientAliveInterval 30
ClientAliveCountMax 3
```

Restart `sshd` to take effect:

```bash
sudo systemctl restart sshd
```

### Only 4/8 GPUs show up in nvidia-smi

Problem description:

Only 4/8 GPUs showes up in `nvidia-smi`. Kernel messege `dmesg` showes:

```log
[   37.010658] NVRM: This PCI I/O region assigned to your NVIDIA device is invalid:
               NVRM: BAR1 is 0M @ 0x0 (PCI:0000:56:00.0)
```

Solution:

```bash
sudo -e /etc/default/grub
```

Edit grub configuration, add kernel boot parameter
`GRUB_CMDLINE_LINUX_DEFAULT="pci=realloc"`

To take effect,

```bash
sudo update-grub
```

Reference:
> https://forums.developer.nvidia.com/t/nvrm-this-pci-i-o-region-assigned-to-your-nvidia-device-is-invalid/229899

### In case you forgot Server BMC (IPMI) password

It is strongly recommended to change the default IPMI password. But what if you forgot your password?

You can use the Supermicro `IPMICFG` tool, which can be downloaded here:
> https://www.supermicro.com/support/faqs/faq.cfm?faq=22134

The default password can be found on the motherboard:
> https://www.supermicro.com/support/BMC_Unique_Password_Guide.pdf

### Prevent Docker and VPN IP address conflicts

The IP range 172.20.0.0/16 is used by the school VPN, but sometimes it is also used by Docker, which has already caused an online accident:

![Docker subnet](images/01_Docker_VPN.png)

![Chat history](images/01_Docker_VPN_02.png)

To prevent these conflicts, add this entity to `/etc/docker/daemon.json`:

```json
{
  // Other configs...,
  "default-address-pools" : [
    {
      "base" : "172.240.0.0/16",
      "size" : 24
    }
  ]
}
```

Remove the conflicting Docker network:

```bash
docker network rm 1d97d71fdb01
```

Then restart Docker to take effect:

```bash
sudo systemctl restart docker
```

P.S. Remember to restart the services related to the removed Docker network.

Reference: [Fixing Docker and VPN IP Address Conflicts](https://www.lullabot.com/articles/fixing-docker-and-vpn-ip-address-conflicts)

### Security Related Rules

- Use strong passwords or private keys
- Use non-default service ports
- User role management
- Backup data & code frequently

1) Change the default SSH port:

    Edit `/etc/ssh/sshd_config`, uncomment:

    ```bash
    #Port 22
    ```

    change it to:

    ```text
    Port 22332
    ```

## Setup Open Source Mirrors & nvidia-docker

### APT

Note: `mirrors.bfsu.edu.cn` is a mirror of `mirrors.tuna.tsinghua.edu.cn`, but a lot faster.

`mirrors.ustc.edu.cn` is also fast but lacks `pypi`, `anaconda`, etc.

> https://mirrors.bfsu.edu.cn/help/ubuntu/
>
> https://mirrors.ustc.edu.cn/help/ubuntu.html

```bash
sudo sed -i 's/archive.ubuntu.com/mirrors.ustc.edu.cn/g' /etc/apt/sources.list
sudo sed -i 's/security.ubuntu.com/mirrors.ustc.edu.cn/g' /etc/apt/sources.list
```

### Python PyPI (pip)

> https://mirrors.bfsu.edu.cn/help/pypi/

```bash
pip config set global.index-url https://mirrors.bfsu.edu.cn/pypi/web/simple
```

## Install Docker-CE

> https://mirrors.bfsu.edu.cn/help/docker-ce/

```bash
sudo apt-get install apt-transport-https ca-certificates curl wget gnupg2 software-properties-common
export DOWNLOAD_URL="https://mirrors.bfsu.edu.cn/docker-ce"
curl -fsSL https://get.docker.com/ | sh
```

> https://docs.docker.com/engine/install/linux-postinstall/

```bash
sudo usermod -aG docker $USER
newgrp docker # Or re-login
```

Also [set up certification for Harbor](./04_Setup_Supplementary_Services.md#harbor).

```bash
sudo mkdir -p /etc/docker/certs.d/harbor.cvgl.lab
cd /etc/docker/certs.d/harbor.cvgl.lab
sudo wget https://cvgl.lab/cvgl.crt --no-check-certificate
```


## Install Nvidia-docker

> https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html

```bash
distribution=$(. /etc/os-release;echo $ID$VERSION_ID) \
      && curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg \
      && curl -s -L https://nvidia.github.io/libnvidia-container/$distribution/libnvidia-container.list | \
            sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
            sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update
sudo apt-get install -y nvidia-docker2
sudo systemctl restart docker
```

## Set up hosts

[link to hosts file](../system-configurations/etc/hosts)

## Configure a Proxy for Docker

### (Deprecated) Setup a temporary proxy service

The open-source project `Project X` originates from XTLS protocol, and provides a set of network tools such as `Xray-core`, supporting the most popular protocols/configurations: `shadowsocks`(deprecated), `vmess`, `vless` and `trojan`. In this section, we will use it to provide an example of how to set up a temporary proxy service.

1) Download and extract the latest release of `Xray-core` [from here](https://github.com/XTLS/Xray-core/releases). (Note that you should choose `Xray-linux-64.zip`)

2) Create a client configuration file `config.json`. An off-the-shelf configuration is available at [here (TBA)](../services/xray/jp-central/config/config.json), which will open SOCKS5 proxy on port `1089` and HTTP proxy on port `8889`. More examples can be found at [XTLS/Xray-examples](https://github.com/XTLS/Xray-examples)

3) Execute `./xray -config ./config.json`.

> Note: Just use the proxy service on the supplementary node (http://10.0.1.68:8889)

### Verify the proxy service

First, launch an HTTP proxy on the node (or in the LAN).

For example, **suppose** you have such a proxy service http://10.0.1.68:8889.

To verify it:

```bash
export https_proxy=http://10.0.1.68:8889
curl https://google.com.hk
```

if there are output lines with an HTTP response like:

```html
<HTML><HEAD><meta http-equiv="content-type" content="text/html;charset=utf-8">
<TITLE>301 Moved</TITLE></HEAD><BODY>
<H1>301 Moved</H1>
The document has moved
<A HREF="https://www.google.com.hk/">here</A>.
</BODY></HTML>
```

it shows that the proxy service is working.

### Configure `Docker` to use the proxy

1) To proceed, recursively create the folder:

    ```sh
    sudo mkdir -p /etc/systemd/system/docker.service.d
    ```

2) Add environment variables to the configuration file `/etc/systemd/system/docker.service.d/proxy.conf`:

    ```conf
    [Service]
    Environment="HTTP_PROXY=http://10.0.1.68:8889"
    Environment="HTTPS_PROXY=http://10.0.1.68:8889"
    Environment="NO_PROXY=localhost,127.0.0.1,nvcr.io,aliyuncs.com,edu.cn,cvgl.lab"
    ```

    You should change `10.0.1.68` and `8889` to the actual proxy address and port respectively.

    Note that the `http` is intentionally used in `HTTPS_PROXY` - this is how most HTTP proxies work.

3) Update configuration and restart `Docker`:

    ```sh
    systemctl daemon-reload
    systemctl restart docker
    ```

4) Check the proxy:

    ```sh
    docker info
    ```

## Install Determined AI Systemwide

```sh
sudo pip install -U pip
sudo pip install -U determined-cli
```

### Pypi cryptography & pyOpenSSL dependency conflict

Problem description:

```log
.
.
.
  File "/usr/local/lib/python3.8/site-packages/OpenSSL/crypto.py", line 3224, in <module>
    utils.deprecated(
TypeError: deprecated() got an unexpected keyword argument 'name'
```

Solution:

```bash
sudo rm -rf /usr/local/lib/python3.8/dist-packages/OpenSSL
sudo rm -rf /usr/local/lib/python3.8/dist-packages/pyOpenSSL-22.1.0.dist-info/
sudo pip install pyOpenSSL==20.0.1 cryptography==36.0.2
```

Reference:
> https://stackoverflow.com/questions/74041308/pip-throws-typeerror-deprecated-error/74046535#74046535

> https://askubuntu.com/questions/1428181/module-lib-has-no-attribute-x509-v-flag-cb-issuer-check/1435520#1435520

## Do stress test

> https://lambdalabs.com/blog/perform-gpu-and-cpu-stress-testing-on-linux

## Maintainance: Upgrade APT packages & `Determined AI`

Warning: Do not upgrade when the cluster is in use! Upgrading packages especially those related to the kernel, DKMS, GPU drivers and containers will kill running tasks.

```sh
sudo apt update
sudo apt upgrade -y

sudo pip install -U pip
sudo pip install -U determined-cli
```

## Common References

### Cluster Management System

- [Determined AI](https://docs.determined.ai/latest/)
- [Microsoft OpenPAI](https://github.com/microsoft/pai)

### System & Design Reference

- [Lambda Echelon GPU Cluster for AI - Reference Design Whitepaper](https://lambdalabs.com/gpu-cluster/echelon)
- [Lambda Labs - How to build a GPU cluster from scratch for your ML team](http://files.lambdalabs.com/How%20to%20build%20a%20GPU%20cluster%20from%20scratch%20for%20your%20ML%20team.pdf)
- [ETH Zürich - Scientific Computing](https://scicomp.ethz.ch/wiki/Main_Page)
- [ETH Zürich - Getting started with scientific clusters](https://scicomp.ethz.ch/w/images/c/c1/Getting_started_with_scientific_clusters_CVL.pdf)
- [CECI documentation](https://support.ceci-hpc.be/doc/index.html)

### Wikis / DBs / KBs

- [ArchWiki](https://wiki.archlinux.org/)
- [TechPowerUp - CPU Specs Database](https://www.techpowerup.com/cpu-specs/)
- [TechPowerUp - GPU Specs Database](https://www.techpowerup.com/gpu-specs/)
- [Intel Product Specifications](https://ark.intel.com/content/www/us/en/ark.html)
- [TrueNAS Docs Hub](https://www.truenas.com/docs/)
- [napp-it ZFS server manual](https://www.napp-it.org/manuals/index_en.html)
- [VMware Knowledge Base](https://kb.vmware.com/s/)
- [NVIDIA Docs](https://docs.nvidia.com/)

### Benchmarks

- [Lambda Labs - Deep Learning GPU Benchmarks](https://lambdalabs.com/gpu-benchmarks)
- [PassMark - CPU Mark](https://www.cpubenchmark.net/high_end_cpus.html)
- [Geekbench - CUDA Results](https://browser.geekbench.com/cuda-benchmarks)
- [Geekbench - Top Multi-Core Geekbench 5 CPU Results](https://browser.geekbench.com/v5/cpu/multicore)
- [AudoDL帮助文档 - 性能实测](https://www.autodl.com/docs/gpu_perf/)

### Forums / Q&As

- [Serve the Home Forum](https://forums.servethehome.com/index.php)
- [Linus Tech Tips Forum](https://linustechtips.com/)
- [Ubuntu Forums](https://ubuntuforums.org/)
- [Arch Linux Forums](https://bbs.archlinux.org/)
- [StackOverflow](https://stackoverflow.com/)
- [StackExchange](https://stackexchange.com/)
- [ServerFault](https://serverfault.com/)
- [AskUbuntu](https://askubuntu.com/)
- [Reddit - HomeServer](https://www.reddit.com/r/HomeServer/)
- [Reddit - HomeLab](https://www.reddit.com/r/homelab/)
- [TrueNAS Community](https://www.truenas.com/community/)

### Media

- [Serve the Home - Homepage](https://www.servethehome.com/)
- [Serve the Home - Youtube](https://www.youtube.com/channel/UCv6J_jJa8GJqFwQNgNrMuww)
- [Linus Tech Tips - Youtube](https://www.youtube.com/@LinusTechTips)
- [Linus Tech Tips - Bilibili](https://space.bilibili.com/12434430)
- [LYYCloud - VMware, K8S](https://new.llycloud.com/)
- [司波图](https://space.bilibili.com/28457)
- [钱韦德](https://space.bilibili.com/20274090)
- [Sagit](https://space.bilibili.com/171459855)
- [无情开评](https://space.bilibili.com/514160575)
- [跟李沐学AI](https://space.bilibili.com/1567748478)
- [科技宅小明](https://space.bilibili.com/5626102)
- [爱折腾的老高](https://space.bilibili.com/455976991/)
