# Per-line standby paths

A fixed proxy port keeps its original VLESS peer and transport identity. Xray probes the direct outbound and each enrolled relay with HTTPS. For new connections it prefers direct, then the first healthy relay in inventory order. The final relay is the fallback when all observations fail or are still initializing. Interrupted connections require a client retry.

Copy `config.example.json` to the ignored `config.json`. Replace the example names, addresses, ports, allowed sources and HTTPS probe URL with site values. A line key is its Xray service directory name. Its host and port must equal the original VLESS peer. Each entry in its `ports` map enrolls it on a named path. Ports must be unique within a path. The path's `address` must resolve from the line container. Omit a line to disable standby for it. Without the file, no lines are enrolled.

From the services directory, generate the configuration for each enrolled relay. For the example inventory:

```sh
mkdir -p xray-ha/standby/generated
python3 xray/scripts/render_standby_relay.py campus xray > xray-ha/standby/generated/forward.json
python3 xray/scripts/render_standby_relay.py remote frps > xray-ha/standby/generated/server.toml
python3 xray/scripts/render_standby_relay.py remote frpc > xray-ha/standby/generated/client.toml
```

Install the forward config on its relay host. Bind its listener on the configured interface and allow the proxy service's source CIDR. For a reverse path, run frps on the proxy side and frpc on the relay side. Publish the control port to the client. Keep the data listeners on the private proxy network. Mount the dedicated CA certificate, endpoint certificate and key, and shared token at the paths in each generated TOML. Use a server certificate matching `control.server_name`. Keep these credentials outside Git.

For existing lines, create `.line-monitor` with mode 0700 inside each enrolled line's config directory, owned by the service operator. Run the existing proxy account render command and restart affected active raw services after installing their relay configurations. To retire a path, remove its line mappings, render and restart the affected lines, then remove the unused relay listeners.

For portal and Grafana status, run `python3 xray/scripts/collect_line_status.py /path/to/textfile/line.prom` at least every 30 seconds. Configure node_exporter's textfile collector to read that directory and Prometheus to load the proxy line recording rules. The status joins the direct observation with the existing fixed-port HTTP and SOCKS probes. All relays share the backup-path label. Probe observations older than 90 seconds expire.

When migrating a previously customized line, first use its original renderer to remove the generated standby routes or restore the unmodified VLESS config from backup. Populate the local inventory before rendering with this implementation. Test the relay configs and an isolated copy of the line before restarting the live service.
