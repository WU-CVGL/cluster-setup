# First-time setup of Cluster Nodes

## Contents

- [First-time setup of Cluster Nodes](#first-time-setup-of-cluster-nodes)
  - [Contents](#contents)
  - [To make the system more reliable](#to-make-the-system-more-reliable)
    - [Disable unattended-updates](#disable-unattended-updates)
    - [Disable GUI](#disable-gui)
    - [Disable network\*-wait-online](#disable-network-wait-online)
    - [Only 4/8 GPUs show up in nvidia-smi](#only-48-gpus-show-up-in-nvidia-smi)
    - [In case you forgot Server BMC (IPMI) password](#in-case-you-forgot-server-bmc-ipmi-password)
    - [Security Related Rules](#security-related-rules)
  - [Setup Open Source Mirrors \& nvidia-docker](#setup-open-source-mirrors--nvidia-docker)
    - [Apt](#apt)
    - [Python PyPI (pip)](#python-pypi-pip)
  - [Install Docker-CE](#install-docker-ce)
  - [Install Nvidia-docker](#install-nvidia-docker)
  - [Configure a Proxy for Docker](#configure-a-proxy-for-docker)
    - [Setup a temporary proxy service](#setup-a-temporary-proxy-service)
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

## To make the system more reliable

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

### Apt

Note: `mirrors.bfsu.edu.cn` is a mirror of `mirrors.tuna.tsinghua.edu.cn`, but a lot faster.

`mirrors.ustc.edu.cn` is also fast but lacks `pypi`, `anaconda`, etc.

> https://mirrors.bfsu.edu.cn/help/ubuntu/

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

## Configure a Proxy for Docker

### Setup a temporary proxy service

The open-source project `Project X` originates from XTLS protocol, and provides a set of network tools such as `Xray-core`, supporting the most popular protocols/configurations: `shadowsocks`(deprecated), `vmess`, `vless` and `trojan`. In this section, we will use it to provide an example of how to set up a temporary proxy service.

1) Download and extract the latest release of `Xray-core` [from here](https://github.com/XTLS/Xray-core/releases). (Note that you should choose `Xray-linux-64.zip`)

2) Create a client configuration file `config.json`. An off-the-shelf configuration is available at [here (TODO)](https://git.cvgl.lab/Cluster_User_Group/CVGL-Services/src/branch/nginx/xray/jp-central/config/config.json), which will open SOCKS5 proxy on port `1089` and HTTP proxy on port `8889`. More examples can be found at [XTLS/Xray-examples](https://github.com/XTLS/Xray-examples)

3) Execute `./xray -config ./config.json`.

### Verify the proxy service

First, launch an HTTP proxy on the node (or in the LAN).

For example, **suppose** you have such a proxy service http://192.168.233.8:18889.

To verify it:

```bash
export https_proxy=http://192.168.233.8:18889
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
    Environment="HTTP_PROXY=http://192.168.233.8:18889"
    Environment="HTTPS_PROXY=http://192.168.233.8:18889"
    Environment="NO_PROXY=localhost,127.0.0.1,nvcr.io,aliyuncs.com,cvgl.lab"
    ```
    You should change `192.168.233.8` and `18889` to the actual proxy address and port respectively.

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

- [Archlinux Wiki](https://wiki.archlinux.org/)
- [TechPowerUp - CPU Specs Database](https://www.techpowerup.com/cpu-specs/)
- [TechPowerUp - GPU Sepcs Database](https://www.techpowerup.com/gpu-specs/)
- [Intel Product Specifications](https://ark.intel.com/content/www/us/en/ark.html)
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
- [跟李沐学AI](https://space.bilibili.com/1567748478)
- [无情开评](https://space.bilibili.com/514160575)
- [司波图](https://space.bilibili.com/28457)
- [钱韦德](https://space.bilibili.com/20274090)
- [Sagit](https://space.bilibili.com/171459855)
- [科技宅小明](https://space.bilibili.com/5626102)
- [爱折腾的老高](https://space.bilibili.com/455976991/)