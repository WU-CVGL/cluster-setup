# Determined Configuration Files

- [Configuration file](../system-configurations/etc/determined/master.yaml) location: /etc/determined/master.yaml

## Master-up command

```bash
det deploy local master-up --master-config-path /etc/determined/master.yaml
```

## Agent-up command

```bash
det deploy local agent-up $DET_MASTER --agent-resource-pool=64c128t_512_3090
```
