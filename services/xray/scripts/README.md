# Xray 服务创建器

自动创建 Xray 服务配置，包括：
- 解析 Xray 分享 URL（vmess://, vless://, trojan://, ss://）
- 生成 Xray 配置文件（包含 API endpoint）
- 更新 docker-compose.yml（添加 xray 和 exporter 服务）
- 更新 prometheus.yml（添加 exporter 目标）
- 检查端口占用

## 使用方法

```bash
cd /home/cvgladmin/ws/cluster-setup/services
python3 xray/scripts/create_xray_service.py <share_url> <service_name> <http_port> <socks_port>
```

### 参数说明

- `share_url`: Xray 分享 URL（支持 vmess://, vless://, trojan://, ss://）
- `service_name`: 简短的服务名称（如: jp-tokyo, us-east）
- `http_port`: HTTP 代理的主机端口（容器内固定使用 8889）
- `socks_port`: SOCKS5 代理的主机端口（容器内固定使用 1089）

### 示例

```bash
# 创建日本东京服务，HTTP 端口 8080，SOCKS5 端口 1080
python3 xray/scripts/create_xray_service.py \
  "vmess://eyJ2IjoiMiIsInBzIjoi..." \
  jp-tokyo \
  8080 \
  1080

# 创建美国东部服务，HTTP 端口 8081，SOCKS5 端口 1081
python3 xray/scripts/create_xray_service.py \
  "vless://uuid@example.com:443?security=tls&sni=example.com#US-East" \
  us-east \
  8081 \
  1081
```

## 功能说明

### 1. URL 解析

支持以下协议：
- **vmess://**: Base64 编码的 JSON 配置
- **vless://**: URL 格式，如 `vless://uuid@host:port?params#remark`
- **trojan://**: URL 格式，如 `trojan://password@host:port?params#remark`
- **ss://**: Shadowsocks URL 格式

### 2. 配置文件生成

生成的配置文件包含：
- 启用的 API endpoint（端口 10085，用于 exporter）
- HTTP 代理（容器内端口 8889）
- SOCKS5 代理（容器内端口 1089）
- 统计功能（用于 Prometheus 监控）
- 路由规则（直连中国和私有 IP）

### 3. 端口检查

脚本会自动检查：
- HTTP 端口是否已被 docker-compose.yml 中的其他服务占用
- SOCKS5 端口是否已被 docker-compose.yml 中的其他服务占用

如果端口被占用，脚本会报错并显示占用该端口的服务名。

### 4. 自动更新配置

脚本会自动：
- 在 `xray/<service_name>/config/config.json` 创建配置文件
- 在 `docker-compose.yml` 中添加 xray 服务和 exporter 服务
- 在 `prometheus/prometheus.yml` 中添加 exporter 目标

## 创建后的步骤

1. **检查配置文件**：
   ```bash
   cat xray/<service_name>/config/config.json
   ```

2. **启动服务**：
   ```bash
   docker-compose up -d xray-<service_name> xray-<service_name>-exporter
   ```

3. **验证服务**：
   ```bash
   # 检查容器状态
   docker-compose ps xray-<service_name>
   
   # 测试 HTTP 代理
   curl -x http://localhost:<http_port> http://www.google.com
   
   # 检查 Prometheus 指标
   curl http://localhost:9090/api/v1/targets | grep <service_name>
   ```

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
grep -r "8080:8889" docker-compose.yml

# 选择其他端口重新运行
python3 xray/scripts/create_xray_service.py <url> <name> 8082 1082
```

### URL 解析失败

如果 URL 解析失败：
- 检查 URL 格式是否正确
- 对于 vmess://，确保是有效的 Base64 编码
- 对于其他协议，确保 URL 格式符合标准

### 配置文件错误

如果生成的配置文件有问题：
- 检查 `xray/<service_name>/config/config.json`
- 参考现有的配置文件（如 `xray/jp-central/config/config.json`）
- 手动修复后重新启动服务

