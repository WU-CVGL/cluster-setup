# Xray 服务创建器

自动创建 Xray 服务配置，包括：
- 解析 Xray 分享 URL（vmess://, vless://, trojan://, ss://）
- 生成 Xray 配置文件（包含 API endpoint）
- 更新 docker-compose.yml（添加 xray 和 exporter 服务）
- 更新 prometheus.yml（添加 exporter 目标）
- 检查服务名称和端口占用

## 使用方法

```bash
cd /home/cvgladmin/ws/cluster-setup/services
python3 xray/scripts/create_xray_service.py <share_url> <service_name> <http_port> <socks_port>
```

### 参数说明

- `share_url`: Xray 分享 URL（支持 vmess://, vless://, trojan://, ss://）
- `service_name`: 简短的服务名称（如: jp-tokyo, us-east）。只能包含小写字母、数字、`-`、`_`，并以字母或数字开头
- `http_port`: HTTP 代理的主机端口（容器内固定使用 8889）
- `socks_port`: SOCKS5 代理的主机端口（容器内固定使用 1089）
- `--base-dir`: 服务根目录（默认为脚本所在的 `services/`；可指向一个副本先试运行）

### 示例

```bash
# 创建日本东京服务，HTTP 端口 56889，SOCKS5 端口 56089
python3 xray/scripts/create_xray_service.py \
  "vmess://eyJ2IjoiMiIsInBzIjoi..." \
  jp-tokyo \
  56889 \
  56089

# 创建美国东部服务，HTTP 端口 41889，SOCKS5 端口 41089
python3 xray/scripts/create_xray_service.py \
  "vless://uuid@example.com:443?security=tls&sni=example.com#US-East" \
  us-east \
  41889 \
  41089
```

现有服务的主机端口一般成对使用 `<x>089`（SOCKS5）和 `<x>889`（HTTP），见 `docker-compose.yml`。

## 功能说明

### 1. URL 解析

支持以下协议：
- **vmess://**: Base64 编码的 JSON 配置
- **vless://**: URL 格式，如 `vless://uuid@host:port?params#remark`
- **trojan://**: URL 格式，如 `trojan://password@host:port?params#remark`
- **ss://**: Shadowsocks URL 格式：`ss://base64(method:password)@host:port#remark`、
  SIP002 明文 userinfo `ss://method:password@host:port#remark`（percent-encode，如 SS-2022），
  以及旧格式 `ss://base64(method:password@host:port)#remark`

说明：
- userinfo（vless 的 id、trojan 的密码、ss 的明文 userinfo）会先做 percent-decode
- 查询参数不能覆盖 URL 本身的地址、端口、id/密码、协议和 remark
- 缺少端口或端口无效（不在 1-65535）时直接报错
- remark 作为 outbound 的 tag；remark 为 `api`、`DIRECT`、`BLACKHOLE`（配置中已使用的 tag）时自动加后缀 `-proxy`

### 2. 配置文件生成

生成的配置文件包含：
- 启用的 API endpoint（端口 10085，用于 exporter）
- HTTP 代理（容器内端口 8889）
- SOCKS5 代理（容器内端口 1089）
- 统计功能（用于 Prometheus 监控）
- 路由规则（直连中国和私有 IP）

模板只有一份：`XrayConfigHandler.build_xray_config`（`vless_to_config.py --stats-api` 用的也是它）。

### 3. 检查

写入任何文件之前，脚本会检查：
- 服务名称格式；`docker-compose.yml` 中是否已有 `xray-<service_name>` 或 `xray-<service_name>-exporter`；
  `xray/<service_name>/` 目录是否已存在（避免覆盖已有的 config.json，它被 gitignore，无法从 git 恢复）
- 端口在 1-65535 之间，且 HTTP 和 SOCKS5 端口不同
- HTTP/SOCKS5 端口是否已被 `docker-compose.yml` 中的服务发布到主机。只比较主机侧端口，
  支持 `80:80`、`"443:443"`、`127.0.0.1:8080:80`、`8080:80/udp`、端口范围和长格式 `published:`。
  安装了 PyYAML 时用 PyYAML 解析，否则用简单的正则解析
- 端口是否在脚本内登记的保留端口中（`RESERVED_HOST_PORTS`）。这些服务在 `docker-compose.yml` 的
  `ports:` 中看不到：frps（`network_mode: host`，7000、7500）、rustdesk hbbs/hbbr（`network_mode: host`，
  21115-21119）、node-exporter（独立的 docker compose，9100）、Harbor（独立的 docker compose，50000）

如果服务或端口已被占用，脚本会报错并显示占用该端口的服务名。

脚本看不到的端口：frp 用户隧道的 `remote_port`（由用户自行选择）以及 Docker 随机分配的主机端口。
在 附属服务 VM（supplementary services VM） 上可以再确认一次：

```bash
ss -ltn | grep ":<port> "
```

### 4. 自动更新配置

脚本会自动：
- 在 `xray/<service_name>/config/config.json` 创建配置文件（并创建 `xray/<service_name>/log/`）
- 在 `docker-compose.yml` 中最后一个 xray exporter 服务之后添加 xray 服务和 exporter 服务
- 在 `prometheus/prometheus.yml` 的 v2ray job 中添加 exporter 目标（第一组 targets，`proxy_role: raw`；`xray-ha-exporter` 在另一组）

脚本不会把新线路加入 HA 入口的线路池，也不会为它添加端到端探测：探测需要手动在 `blackbox/blackbox.example.yml`
中添加 `<service_name>_http` 和 `<service_name>_socks` 两个 module，并在 `prometheus.yml` 的 `proxy-probe`
job 中添加这两个目标（`pool: none`），然后运行 `proxy_accounts.py render` 并
`docker compose kill -s SIGHUP blackbox-exporter`，见 [docs/00](../../../docs/00_Network_Proxy.md#proxy-availability)。

三个文件的新内容都先在内存中生成并检查（安装了 PyYAML 时还会确认除新增内容外文件没有其他变化），
全部通过后才写入。写入过程中出错时，已写入的文件会被恢复，新建的目录会被删除。

## 创建后的步骤

以下命令都在 `services/` 目录下执行：

1. **检查配置文件**：
   ```bash
   cat xray/<service_name>/config/config.json
   ```

2. **启动服务**：
   ```bash
   docker compose up -d xray-<service_name> xray-<service_name>-exporter
   ```

3. **重新加载 Prometheus 配置**（Prometheus 不会自动读取修改后的 prometheus.yml）：
   ```bash
   docker compose kill -s SIGHUP prometheus
   ```

4. **验证服务**：
   ```bash
   # 检查容器状态
   docker compose ps xray-<service_name> xray-<service_name>-exporter
   
   # 测试 HTTP / SOCKS5 代理（raw-auth on 时用 http://<user>:<password>@localhost:<http_port>
   # 和 socks5h://<user>:<password>@localhost:<socks_port>）
   curl -x http://localhost:<http_port> -I https://www.google.com
   curl --socks5-hostname localhost:<socks_port> -I https://www.google.com
   
   # 检查 Prometheus 目标（Prometheus 没有发布主机端口，在容器内查询）
   docker compose exec prometheus wget -qO- http://localhost:9090/api/v1/targets | grep <service_name>
   ```

## 单独生成配置: vless_to_config.py

`vless_to_config.py` 只把一个 `vless://` 链接转换成完整的 Xray 配置 JSON，不修改任何其他文件：

```bash
python3 xray/scripts/vless_to_config.py "vless://..." -o /tmp/config.json
```

如果要把输出用作已有 compose 服务的 `xray/<service_name>/config/config.json`（例如更换节点），
必须加 `--stats-api`（exporter 需要统计 API），并保持 `--http-port`/`--socks-port` 的默认值：
这两个是容器内的端口（8889/1089），不是 `docker-compose.yml` 中的主机端口。

## 代理账号: proxy_accounts.py

HA 入口（以及开启后的各线路自身入口）要求代理账号。账号保存在一个文件 `services/proxy-accounts/accounts.json`
（权限 `0600`，被 gitignore）；所有含密码的配置都由 `render` 从这个文件生成，不手动维护第二份密码
（用法和上线顺序见 [docs/00](../../../docs/00_Network_Proxy.md#accounts)）：

```bash
cd services
python3 xray/scripts/proxy_accounts.py add <name> [--show]      # 新账号，随机密码（secrets）；--show 时输出密码
python3 xray/scripts/proxy_accounts.py rotate <name> [--show]   # 换密码
python3 xray/scripts/proxy_accounts.py remove <name>
python3 xray/scripts/proxy_accounts.py list                     # 只输出账号名
python3 xray/scripts/proxy_accounts.py raw-auth [on|off]        # 线路自身入口是否要求账号，默认 off
python3 xray/scripts/proxy_accounts.py render
```

- 账号名：集群用户名，小写字母、数字、`.`、`_`、`-`，以字母或数字开头，最长 64 个字符。`svc-` 开头的是服务账号，
  只能是 `svc-ha`（HA 入口登录线路）、`svc-monitor`（探测）和 `svc-docker-<node>`（节点的 Docker daemon）
- `render` 先在内存中生成全部文件并检查，再逐个原子替换（同目录临时文件 + rename，权限 `0600`），内容未变的文件不写：
  - `xray-ha/config/config.json`：`xray-ha/config.example.json` 的 HTTP 和 SOCKS5 入站换成除 `svc-ha` 外的所有账号
    （SOCKS5 `auth: password`）；`raw-auth on` 时 `pool-*` 出站以 `svc-ha` 登录线路
  - `blackbox/config/blackbox.yml`：`blackbox/blackbox.example.yml` 中经过 `xray-ha` 的 `proxy_url` 带上 `svc-monitor`
    （percent-encode）；`raw-auth on` 时各线路的 `proxy_url` 也带上
  - `xray/*/config/config.json`：`raw-auth on` 时入站要求所有账号（含 `svc-ha`），`off` 时带账号的入站恢复为无认证，
    其他文件不动。
    修改用的是 `XrayConfigHandler.set_inbound_users`，`build_xray_config(..., inbound_users=...)` 也用它；
    不传 `inbound_users` 时 HTTP/SOCKS5 入站不要求认证（SOCKS5 `auth: noauth`）
- 缺少 `svc-monitor`（`raw-auth on` 时还有 `svc-ha`）或某个线路配置没有 HTTP/SOCKS5 入站时，`render` 报错，不写任何文件
- 之后重启 `xray-ha`（`raw-auth on` 时连同所有线路一起），并 `docker compose kill -s SIGHUP blackbox-exporter`

`raw-auth on` 时，`create_xray_service.py` 新建的线路在创建时就要求所有账号（配置文件权限 `0600`）；账号文件中没有账号时拒绝创建。

## 测试

```bash
cd services/xray/scripts
python3 -B -m unittest discover -s tests -v
```

`tests/fixtures/cases.json` 记录了每个分享链接预期的解析和 outbound 结果（已与重构前 f71d24c 的输出逐个核对，
有意的差异列在 `tests/corpus.py` 中），生成的 `config.json` 与 `tests/fixtures/` 中的模板逐字节比较。

## 文件结构

创建后的文件结构：

```
services/
├── xray/
│   └── <service_name>/
│       ├── config/
│       │   └── config.json
│       └── log/
├── docker-compose.yml (已更新)
└── prometheus/
    └── prometheus.yml (已更新)
```

## 注意事项

1. **端口映射**：
   - 容器内固定使用端口 1089（SOCKS5）和 8889（HTTP）
   - 主机端口由用户指定，映射到容器端口

2. **服务命名**：
   - Docker 服务名格式：`xray-<service_name>`
   - Exporter 服务名格式：`xray-<service_name>-exporter`

3. **网络**：
   - 所有 xray 服务都在 `grafana_monitor` 网络中
   - Exporter 通过 Docker 网络访问 xray API（端口 10085）

4. **备份**：
   - 建议在运行脚本前备份 `docker-compose.yml` 和 `prometheus.yml`

## 故障排除

### 端口已被占用

如果遇到端口占用错误：
```bash
# 查找占用端口的服务
grep -n "<port>" docker-compose.yml
ss -ltn | grep ":<port> "

# 选择其他端口重新运行
python3 xray/scripts/create_xray_service.py <url> <name> 46889 46089
```

### URL 解析失败

如果 URL 解析失败：
- 检查 URL 格式是否正确
- 对于 vmess://，确保是有效的 Base64 编码
- 对于其他协议，确保 URL 格式符合标准（包括端口）

### 配置文件错误

如果生成的配置文件有问题：
- 检查 `xray/<service_name>/config/config.json`
- 参考 附属服务 VM（supplementary services VM） 上其他服务的配置文件（`xray/<name>/config/config.json`，被 gitignore，不在仓库中）
- 手动修复后重新启动服务
