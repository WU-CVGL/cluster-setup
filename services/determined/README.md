# Determined Configuration Files

- Configuration file location: /etc/determined

## Master-up command

```bash
det deploy local master-up --master-config-path /etc/determined/master.yaml
```

## Agent-up command

```bash
det deploy local agent-up $DET_MASTER --agent-resource-pool=64c128t_512_3090
```
