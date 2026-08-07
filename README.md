# Observability MCP

这是一个可观测性 MCP 服务，让 AI 模型可以查询：

- 正式环境的 Elasticsearch 日志
- 测试环境的 SkyWalking 日志和 Trace
- Prometheus 或 VictoriaMetrics 指标

服务使用 Streamable HTTP，MCP 地址为：

```text
http://你的服务器IP:8000/mcp
```

健康检查地址为：

```text
http://你的服务器IP:8000/health
```

## 1. 准备配置

项目配置放在 `.env` 文件中。先复制示例：

```bash
cp .env.example .env
```

然后编辑 `.env`：

```bash
nano .env
```

至少需要检查下面这些配置：

```dotenv
# MCP 服务监听地址
MCP_HOST=0.0.0.0
MCP_PORT=8000

# Elasticsearch，多个地址使用英文逗号分隔
ES_URLS=http://es-node-1:9200,http://es-node-2:9200
ES_AUTH_TYPE=basic
ES_USERNAME=your-user
ES_PASSWORD=your-password
ES_DEFAULT_INDEX=*
ES_ALLOWED_INDEX_PATTERNS=*

# Prometheus 或 VictoriaMetrics
METRICS_API_BASE_URL=http://prometheus:9090/api/v1
METRICS_AUTH_TYPE=none

# SkyWalking OAP GraphQL 地址
SKYWALKING_GRAPHQL_URL=http://skywalking-oap:12800/graphql
SKYWALKING_AUTH_TYPE=none
```

认证方式支持：

- `none`：不认证
- `basic`：用户名和密码
- `bearer`：Bearer Token
- `api_key`：API Key

`.env` 中可能包含密码，不要把它提交到 Git 或发到公开环境。

## 2. Linux 使用 Python 启动

### 环境要求

- Linux
- Python 3.11 或更高版本
- 可以访问 Elasticsearch、SkyWalking 和指标后端

### 安装

进入项目目录：

```bash
cd /path/to/mxmcp
```

创建 Python 虚拟环境：

```bash
python3 -m venv .venv
```

启用虚拟环境：

```bash
source .venv/bin/activate
```

安装依赖：

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

将 `.env` 加载为环境变量：

```bash
set -a
source .env
set +a
```

启动服务：

```bash
python -m observability_mcp
```

看到服务监听 `8000` 端口后，打开另一个终端验证：

```bash
curl http://127.0.0.1:8000/health
```

停止服务时按 `Ctrl+C`。

## 3. 使用 Docker Compose 启动

### 环境要求

- Docker
- Docker Compose

确认项目目录中已经有配置好的 `.env`，然后执行：

```bash
docker compose up -d --build
```

查看运行状态：

```bash
docker compose ps
```

查看日志：

```bash
docker compose logs -f observability-mcp
```

验证服务：

```bash
curl http://127.0.0.1:8000/health
```

停止服务：

```bash
docker compose down
```

更新代码后重新构建：

```bash
docker compose up -d --build
```

注意：容器中的 `127.0.0.1` 指向容器自己。如果 Elasticsearch、SkyWalking
或 Prometheus 在其他机器上，`.env` 中应填写那台机器可访问的 IP 或域名。

## 4. 连接 MCP 客户端

在支持 Streamable HTTP 的 MCP 客户端中填写：

```text
http://服务器IP:8000/mcp
```

如果 MCP 客户端和服务在同一台机器，可以使用：

```text
http://127.0.0.1:8000/mcp
```

环境选择规则已经写入 MCP instructions：

- 用户提到测试环境或测试线：查询 SkyWalking
- 用户提到正式环境或生产环境：查询 Elasticsearch
- 用户没有说明环境：默认查询正式环境 Elasticsearch
- 查询无结果时不会自动跨环境重试，而是提示确认环境

## 5. MCP 工具

### 日志工具

- `list_log_indices`：列出 Elasticsearch 索引、Alias 或 Data Stream
- `search_logs`：查询正式环境 Elasticsearch 日志
- `list_skywalking_services`：列出测试环境 SkyWalking 服务
- `search_skywalking_logs`：查询测试环境 SkyWalking 日志
- `get_skywalking_trace`：查询测试环境 SkyWalking Trace

`search_logs` 支持的常用条件：

- `trace_id`
- `node_ip`
- `keyword`
- `level`
- `start_time`、`end_time`
- `index`
- `limit`

建议尽量指定索引和时间范围。例如：

```json
{
  "level": "ERROR",
  "node_ip": "zp-llm-app-12-193",
  "index": "applog-*",
  "start_time": "2026-08-07T00:00:00+08:00",
  "end_time": "2026-08-07T23:59:59+08:00",
  "limit": 20
}
```

如果响应中 `has_more` 为 `true`，下一页只传 `next_cursor`：

```json
{
  "cursor": "lc_xxxxxxxxxxxxxxxxxxxxxxxx"
}
```

### 指标工具

- `query_instant`：查询某个时刻的 PromQL
- `query_range`：查询一段时间内的 PromQL
- `get_label_values`：查询某个标签的可选值

## 6. 日志 Profile

日志字段配置位于：

```text
config/log-profiles.yaml
```

Profile 用来告诉服务不同索引中的时间、消息、级别、Trace ID、节点和错误堆栈
分别存在哪些字段中。调用者不需要传 Profile，服务会根据索引自动选择。

默认规则包括：

- `filebeat-*`、`logs-ecs-*`：使用 `ecs`
- `applog-*`：使用 `offset-log`
- 其他索引：使用 `generic`

修改 Profile 后需要重启 MCP 服务。

## 7. 常见问题

### 服务无法启动

先检查 Python 版本和依赖：

```bash
python --version
python -m pip install -r requirements.txt
```

再确认 `.env` 已经加载。Linux Python 启动方式需要先执行：

```bash
set -a
source .env
set +a
```

### 无法连接 Elasticsearch 或指标后端

从 MCP 所在机器测试目标地址：

```bash
curl http://目标地址:端口
```

同时检查用户名、密码、防火墙和网络路由。

### 无法连接 SkyWalking

应连接 OAP 的 HTTP/GraphQL 端口，通常是 `12800`：

```dotenv
SKYWALKING_GRAPHQL_URL=http://OAP地址:12800/graphql
```

`30000` 通常是 SkyWalking UI 端口，不是首选的 OAP GraphQL 地址。

### 日志查询很慢

避免同时使用下面两个条件：

- `index=*`
- 不设置开始和结束时间

推荐指定较小的索引范围和时间范围。`limit=5` 只限制返回数量，不代表
Elasticsearch 只检查 5 条数据。

## 8. 开发测试

安装开发依赖：

```bash
python -m pip install -r requirements-dev.txt
```

运行测试和代码检查：

```bash
python -m pytest -q
python -m ruff check .
```

## License

MIT
