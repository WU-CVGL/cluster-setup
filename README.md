# Welcome to the CVGL-Lab cluster

This is a manual for system admin.

## Quick Guide

[1. First-time Setup of Cluster Nodes](docs/01_First-time_Setup_of_Cluster_Nodes.md)

[2. User Management](docs/02_User_Management.md)

[3. Setup DeterminedAI](docs/03_Setup_DeterminedAI.md)

[4. Setup Supplementary Services](docs/04_Setup_Supplementary_Services.md)

## Cluster Information

Our cluster is located in the core server room, E6-106.

We have been designated with an IP address range: `10.0.1.64/27`, where the gateway: `10.0.1.65`, address pool: `10.0.1.66-94`.

Our private 10GbE network IP address range: `192.168.233.0/24`

Current IP assignment:

| Name | Spec |
|:---- | ----:|
|10.0.1.66 / 192.168.233.6|core node on storage server|
|10.0.1.67 / 192.168.233.10|login node on storage server|
|10.0.1.68 / 192.168.233.8|supplementary service node on storage server|
|10.0.1.70|ESXi on storage server|
|10.0.1.71 / 192.168.233.11|GPU Node 01|
|10.0.1.72 / 192.168.233.12|GPU Node 02|
|10.0.1.73 / 192.168.233.13|GPU Node 03|
|10.0.1.74 / 192.168.233.14|GPU Node 04|
|10.0.1.80|BMC of storage server|
|10.0.1.81|BMC of GPU Node 01|
|10.0.1.82|BMC of GPU Node 02|
|10.0.1.83|BMC of GPU Node 03|
|10.0.1.84|BMC of GPU Node 04|
|10.0.1.90 / 192.168.233.233(passthru) / 192.168.233.234(virtual)|TrueNAS on storage server|

TODO: Add photo here

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

### Hardware Information

GPU Node 1：

|  Name  |  Spec  |
| :----: | :----  |
|  Model | Powerleader PR4908R (Supermicro 4124GS-TNR)|
|  CPU   | AMD EPYC 7302*2 (32C/64T, 3.0-3.3GHz)|
| Memory | Samsung DDR4 256G (16G*16) 2933MHz ECC REG|
|  GPU   | MSI RTX 3090 Turbo * 8 |
|  SSD   | Intel P4510 2TB * 1 |
|  NIC   | Intel 82599 10GbE   |

GPU Node 2, 3, 4:

|  Name  |  Spec  |
| :----: | :----  |
|  Model | Powerleader PR4908R (Supermicro 4124GS-TNR)|
|  CPU   | AMD EPYC 7402*2 (48C/96T, 2.8-3.35GHz)|
| Memory | SK Hynix / Samsung / Samsung DDR4 512G (32G*16) 3200MHz ECC REG|
|  GPU   | NVIDIA / MSI / MSI RTX 3090 * 8 |
|  SSD   | Intel P4510 2TB * 1 |
|  NIC   | Intel 82599 10GbE   |

Storage & Management node

|  Name  |  Spec  |
| :----: | :----  |
|  Model | Powerleader PR4224AK (Supermicro H11SSL)|
|  CPU   | AMD EPYC 7302*2 (32C/64T, 3.0-3.3GHz)|
| Memory | Samsung DDR4 256G (32G*8) 2933MHz ECC REG |
|  SSD   | Samsung 970 EVO Plus 500G * 1|
|  SSD   | Intel S4510 1.92TB * 2 |
|  HDD   | Seagate Exos X18 18TB * 14 |
|  NIC   | Intel 82599 10GbE Dual Port |