# Determined Configuration Files

- [Configuration file](../system-configurations/etc/determined/master.yaml) location: /etc/determined/master.yaml

## Master-up command

```bash
det deploy local master-up --master-config-path /etc/determined/master.yaml
```

## Agent-up command

```bash
det deploy local agent-up $DET_MASTER --agent-resource-pool=<pool>
```

`<pool>` must be one of the resource pools defined in `master.yaml`:

| Pool | Hardware (from `master.yaml`) |
| :--- | :--- |
| `32c64t_256_3090` | 2x AMD Epyc 7302, 256 GiB RAM, RTX 3090 |
| `48c96t_512_3090` | 2x AMD Epyc 7402, 512 GiB RAM, RTX 3090 |
| `48c96t_512_4090` | 2x AMD Epyc 7402, 512 GB RAM, RTX 4090 |
| `64c128t_512_4090` | 2x AMD Epyc 7543, 512 GB RAM, RTX 4090 |
| `128c256t_1536_4090` | 2x AMD Epyc 9554, 1536 GB RAM, RTX 4090 |
| `128c256t_1536_6000Ada` | 2x AMD Epyc 9554, 1536 GB RAM, RTX 6000 Ada |

Pick the pool that matches the node's hardware (see the hardware tables in the [top-level README](../../README.md#hardware-information)) and check the current assignment with `det agent list` before restarting an agent.
