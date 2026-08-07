# Observability MCP 使用案例

本文面向 MCP 使用者，介绍配置 Observability MCP 端点后可以完成哪些工作，以及如何向 Codex 等 MCP 客户端发起请求。

## 1. 配置 MCP 端点

服务部署后，Streamable HTTP 端点通常是：

```text
http://服务器地址:8000/mcp
```

如果 Docker 使用：

```bash
-p 8888:8000
```

客户端地址应填写：

```text
http://服务器地址:8888/mcp
```

Codex `config.toml` 示例：

```toml
[mcp_servers.observability]
url = "http://服务器地址:8888/mcp"
```

重新连接后，客户端应能看到八个工具：

| 工具 | 能做什么 |
| --- | --- |
| `list_log_indices` | 查看白名单范围内的日志索引、别名和数据流 |
| `search_logs` | 查询 Elasticsearch 日志并翻页 |
| `list_skywalking_services` | 查看 SkyWalking 服务 |
| `search_skywalking_logs` | 查询 SkyWalking 日志 |
| `get_skywalking_trace` | 查询 SkyWalking 调用链 |
| `query_instant` | 查询当前或指定时间点的监控指标 |
| `query_range` | 查询一段时间内的指标趋势 |
| `get_label_values` | 查看应用、实例、任务等标签的可见值 |

可以先对客户端说：

```text
列出当前 MCP 提供的工具，并说明每个工具的用途。
```

## 2. 配置完成后能做什么

配置 ES、SkyWalking 和指标后端后，可以完成以下工作：

- 根据 traceId 还原一次请求的日志链路。
- 根据关键词、日志级别和时间范围搜索日志。
- 查询多个索引，并通过游标继续获取下一页。
- 查询应用当前请求量、错误率、延迟、CPU、内存和线程数。
- 查询最近一小时、一天或一周的指标趋势。
- 查询 SkyWalking 服务日志，并从日志 traceId 继续查看完整调用链。
- 比较不同实例的负载和错误情况。
- 根据异常指标继续检索对应时间段的日志。
- 在不同日志字段结构之间自动选择 Profile，并返回统一日志结构。

### 查看允许的日志索引

对应工具：`list_log_indices`

不传参数时列出 `ES_ALLOWED_INDEX_PATTERNS` 范围内的搜索目标。也可以传入更窄的
`index` 表达式和 `limit`：

```json
{
  "index": "logs-orders-*",
  "limit": 100
}
```

结果中的 `type` 为 `index`、`alias` 或 `data_stream`；`truncated=true` 表示还有结果
因数量上限未返回。Elasticsearch 内置索引默认不会返回；仅在明确需要时传入：

```json
{
  "include_system": true,
  "limit": 100
}
```

### 查询 SkyWalking 日志

先调用 `list_skywalking_services` 确认完整服务名，再调用：

```json
{
  "service_name": "orders-api",
  "keyword": "connection timeout",
  "start_time": "2026-08-05T14:00:00+08:00",
  "end_time": "2026-08-05T15:00:00+08:00",
  "limit": 50
}
```

根据日志中的 traceId 查询调用链：

```json
{
  "trace_id": "0123456789abcdef0123456789abcdef"
}
```

MCP 不会自动判断业务指标是否一定异常。客户端会根据查询结果、历史趋势和你提供的阈值进行分析。

## 3. 查询当前有哪些应用

自然语言：

```text
查询监控系统中 application 标签目前有哪些值。
```

对应工具：`get_label_values`

```json
{
  "label": "application"
}
```

查询实例：

```text
查询监控系统中有哪些 instance。
```

```json
{
  "label": "instance"
}
```

`get_label_values` 不支持 matcher。如果只想查询某个应用的实例，可以使用 `query_instant`：

```json
{
  "promql": "count by (instance) (process_uptime_seconds{application=\"example-service\"})"
}
```

## 4. 分析一个应用的当前状态

自然语言：

```text
分析 example-service 当前的请求量、5xx、平均响应时间、CPU和Heap使用率，按实例展示，并指出需要关注的值。
```

客户端通常会多次调用 `query_instant`。

当前请求量：

```json
{
  "promql": "sum(rate(http_server_requests_seconds_count{application=\"example-service\"}[5m]))"
}
```

按实例统计请求量：

```json
{
  "promql": "sum by (instance) (rate(http_server_requests_seconds_count{application=\"example-service\"}[5m]))"
}
```

5xx比例：

```json
{
  "promql": "sum(rate(http_server_requests_seconds_count{application=\"example-service\",status=~\"5..\"}[5m])) / sum(rate(http_server_requests_seconds_count{application=\"example-service\"}[5m]))"
}
```

平均响应时间：

```json
{
  "promql": "sum(rate(http_server_requests_seconds_sum{application=\"example-service\"}[5m])) / sum(rate(http_server_requests_seconds_count{application=\"example-service\"}[5m]))"
}
```

系统 CPU：

```json
{
  "promql": "system_cpu_usage{application=\"example-service\"}"
}
```

Heap使用率：

```json
{
  "promql": "sum by (instance) (jvm_memory_used_bytes{application=\"example-service\",area=\"heap\"}) / sum by (instance) (jvm_memory_max_bytes{application=\"example-service\",area=\"heap\"})"
}
```

指标名称和标签取决于实际采集配置。如果查询结果为空，应先在 Grafana 或指标系统确认真实指标名称。

## 5. 查询指标趋势

自然语言：

```text
查询 example-service 最近一小时每个实例的CPU趋势，每分钟一个点，找出峰值最高的实例。
```

对应 `query_range`：

```json
{
  "promql": "system_cpu_usage{application=\"example-service\"}",
  "start": "2026-08-04T09:00:00+08:00",
  "end": "2026-08-04T10:00:00+08:00",
  "step": "1m"
}
```

时间支持：

- ISO 8601，例如 `2026-08-04T10:00:00+08:00`
- Unix时间戳

`step` 越小，数据点越多。长时间范围建议使用 `5m`、`15m` 或更大的步长。

## 6. 按 traceId 查询日志

自然语言：

```text
在 logs-orders-* 中查询 traceId 为 9d7d6f2e4a794c5e 的日志，按配置顺序返回50条。
```

对应 `search_logs`：

```json
{
  "trace_id": "9d7d6f2e4a794c5e",
  "index": "logs-orders-*",
  "limit": 50
}
```

每条返回日志都包含 `node_ip`。服务优先读取 `host.ip`；旧版 Filebeat 日志会回退到
`host.name`、`beat.hostname` 或 `beat.name`，例如：

```json
{
  "timestamp": "2026-08-05T06:37:46.628Z",
  "project": "orders-api",
  "node_ip": "node-a",
  "trace_id": "0123456789abcdef0123456789abcdef",
  "level": "INFO",
  "message": "..."
}
```

ERROR 日志还可能包含结构化错误和 Profile 白名单中的扩展属性：

```json
{
  "error": {
    "type": "ExampleException",
    "message": "request failed",
    "stack_trace": "ExampleException: request failed\n...",
    "stack_trace_truncated": false
  },
  "attributes": {
    "span_id": "0123456789abcdef",
    "logger": "example.OrderService",
    "thread": "http-worker-1",
    "log_source": "/var/log/example.log"
  }
}
```

如果堆栈超过 `LOG_MAX_STACK_TRACE_LENGTH`，响应会保留堆栈头尾，并将
`stack_trace_truncated` 设置为 `true`。

traceId 查询会：

1. 查询 Profile 中的 `trace_id.exact` 字段。
2. 查询 Profile 中的 `trace_id.text` 字段。
3. 在 message候选字段中兜底搜索。

因此既能处理独立 traceId 字段，也能处理只把 traceId 写在日志正文中的情况。

## 7. 查询某个时间段的错误日志

自然语言：

```text
查询 logs-orders-* 在2026年8月4日9点到10点之间的ERROR日志，包含connection timeout，返回前100条。
```

```json
{
  "keyword": "connection timeout",
  "level": "ERROR",
  "start_time": "2026-08-04T09:00:00+08:00",
  "end_time": "2026-08-04T10:00:00+08:00",
  "index": "logs-orders-*",
  "limit": 100
}
```

这些条件是 AND 关系：

```text
关键词命中 AND 日志级别命中 AND 日志时间在范围内
```

## 8. 查询多个索引

```json
{
  "keyword": "NullPointerException",
  "index": "logs-orders-*,logs-payment-*",
  "limit": 100
}
```

前提是所有索引都在 `ES_ALLOWED_INDEX_PATTERNS` 白名单内，并且可以使用同一个日志 Profile。

如果多个索引结构不同，建议按索引表达式分别查询，服务会自动选择 Profile：

```json
{
  "keyword": "timeout",
  "index": "filebeat-*"
}
```

## 9. 继续获取下一页日志

首个响应可能包含：

```json
{
  "total": 235,
  "total_relation": "eq",
  "count": 50,
  "has_more": true,
  "next_cursor": "lc_xxxxxxxxxxxxxxxxxxxxxxxx"
}
```

继续查询时只传：

```json
{
  "cursor": "lc_xxxxxxxxxxxxxxxxxxxxxxxx"
}
```

不要重复传入：

- `trace_id`
- `node_ip`
- `keyword`
- `level`
- `start_time`、`end_time`
- `index`
- `limit`

游标是短期、一次性的。默认120秒内未使用会失效，服务重启后也会失效。

自然语言可以说：

```text
如果日志还有下一页，请继续调用游标，最多再查询3页，并总结重复出现的错误。
```

是否自动继续取下一页由 MCP 客户端和模型决定，不是 MCP 服务主动推送。

## 10. 指标异常后继续查日志

完整排查示例：

```text
分析 example-service 最近一小时的5xx趋势。
如果发现错误升高，确定异常开始时间和异常实例；
然后在允许的日志索引中查询对应时间段的ERROR日志，
按错误特征分类，并给出最可能的原因。
```

典型执行过程：

1. `query_range` 查询最近一小时5xx趋势。
2. `query_instant` 按实例统计错误量。
3. 确定异常时间范围和实例。
4. `search_logs` 查询相同时间范围的ERROR日志。
5. 如果 `has_more=true`，继续使用游标。
6. 汇总异常堆栈、下游超时、数据库错误等模式。

可以使用 `node_ip` 按 Profile 配置的 `host.ip`、`host.name`、
`beat.hostname.keyword` 等机器字段过滤日志。

## 11. 常用自然语言问题

```text
查看当前有哪些应用和实例。
```

```text
分析 example-service 最近一小时的请求量、5xx比例、平均耗时、CPU和Heap。
```

```text
比较 example-service 所有实例的CPU和错误量，找出异常实例，不要限制为Grafana当前选中的实例。
```

```text
查询最近30分钟ERROR和WARN日志数量，并按错误特征分类。
```

```text
根据 traceId 9d7d6f2e4a794c5e 还原完整调用过程，如果有下一页继续查询。
```

```text
检查最近24小时是否发生进程重启，并查询重启前后10分钟日志。
```

```text
找出平均耗时最高的10个URI，并判断是高流量慢接口还是低频长连接接口。
```

## 12. 使用注意事项

- 尽量指定日志索引，避免对大量分片创建PIT。
- 默认不要限制某个实例，除非排查目标明确。
- Grafana导出的当前变量值只是界面保存状态，不一定代表分析范围。
- 指标查询为空不等于指标值为0，可能是指标名、标签、租户或数据源不匹配。
- `total_relation=eq` 表示日志总数精确；`gte` 表示至少有这么多条。
- `truncated=true` 表示指标结果因序列数或数据点上限被截断。
- `/health` 成功只代表MCP进程存活，不代表ES和指标后端连接正常。
- MCP端点应部署在HTTPS、认证和访问控制之后。
- 日志和指标可能包含敏感信息，不要直接粘贴到公开Issue或公共仓库。
