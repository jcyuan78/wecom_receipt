# 调试代理模式

> 场景：生产服务器有公网地址，开发机在内网。生产服务器**只转发**企业微信流量，真正的验签、下载、识别、回复都在开发机上运行。

---

## 1. 架构

```
企业微信
   │  回调（加密 POST / URL 校验）
   ▼
生产服务器 :8080  WECOM_MODE=proxy
   │  原样转发 /wecom/callback
   ▼
开发机 :8081      完整程序（normal 模式）
   │  /debug/wecom-api/cgi-bin/*
   ▼
生产服务器  ──▶  qyapi.weixin.qq.com
```

- 生产服务器不解析消息、不入库、不下载文件，只做 HTTP 转发。
- 开发机完成验签、解密、下载、识别、入库和回复；被动回复沿原路返回企业微信。
- 开发机不需要公网 IP，也不需要把出口 IP 加进「企业可信IP」——API 请求由生产服务器转发。

---

## 2. 生产服务器配置

`.env`：

```ini
WECOM_MODE=proxy
WECOM_DEBUG_PROXY_TARGET=http://192.168.1.23:8081
WECOM_DEBUG_PROXY_TOKEN=请换成一串足够长的随机字符串
```

说明：

- proxy 模式下**不再要求** CorpID / AgentId / Secret / Token / EncodingAESKey，可以留空；保留也不影响。
- 确保服务器能访问开发机的 8081 端口（防火墙 / 安全组放行）。
- 企业微信后台的回调 URL **保持不变**，仍指向生产服务器 `/wecom/callback`。
- 启动后访问 `http://127.0.0.1:8080/healthz`，应看到：

  ```json
  {"status":"ok","mode":"proxy","target":"http://192.168.1.23:8081"}
  ```

---

## 3. 开发机配置

把生产的 `.env` 复制到开发机，保留企业微信凭据（验签、解密、回复需要），修改以下项：

```ini
WECOM_MODE=normal
WECOM_DATA_DIR=data-dev
WECOM_PORT=8081

# 关键：把发往企业微信的 API 指向生产服务器的调试代理
WECOM_WECOM_API_BASE=http://<生产服务器IP>:8080/debug/wecom-api
WECOM_DEBUG_PROXY_TOKEN=与生产服务器相同的令牌
```

注意：

- **不要**把开发机的 `WECOM_DATA_DIR` 指向生产数据目录或生产数据库。
- CorpID / AgentId / Secret / Callback Token / EncodingAESKey 必须与生产一致，否则验签失败。
- 启动 `.\run.ps1`，确认监听 `0.0.0.0:8081`，且服务器可访问。
- 开发机的 `WECOM_NOTIFY_RESULTS`、`WECOM_PROCESSOR=auto`、Ollama 等按需配置。

---

## 4. “切换接收源和发送目标”

| 方向 | 由谁决定 | 配置 |
|---|---|---|
| 接收（企业微信 → 开发机） | 生产服务器 | `WECOM_DEBUG_PROXY_TARGET` 指向开发机 |
| 发送（开发机 → 企业微信） | 开发机 | `WECOM_WECOM_API_BASE` 指向生产服务器 `/debug/wecom-api` |

开发机不需要改企业微信后台，也不需要公网暴露；切换回生产时，把生产服务器改回 `WECOM_MODE=normal` 并重启即可。

---

## 5. 安全要求

- `WECOM_DEBUG_PROXY_TOKEN` 必须足够长、随机，服务器启动时会强制校验非空。
- `/debug/wecom-api` 只在 proxy 模式存在，且必须携带 `X-Debug-Token`；错误令牌返回 401。
- 建议用防火墙只允许开发机访问 `/debug/wecom-api`，或走 VPN。
- 本项目的 `run.ps1` 已加 `--no-access-log`，避免 `corpsecret` / `access_token` 出现在访问日志里。
- 回调转发本身不做验签，由开发机校验企业微信签名；生产服务器只是通道。
- 调试完成后请关闭 proxy 模式，避免长期开放转发入口。

---

## 6. 限制与排查

| 现象 | 说明 / 处理 |
|---|---|
| 502 Bad Gateway | 服务器连不上开发机：检查 IP、端口、防火墙、开发机是否启动 |
| 401 invalid debug token | 两端 `WECOM_DEBUG_PROXY_TOKEN` 不一致 |
| 开发机日志 403 签名失败 | 开发机的 Token / EncodingAESKey / CorpID 与生产不一致 |
| 60020 | 开发机是否把 `WECOM_WECOM_API_BASE` 指到了调试代理；代理出口 IP 是否在可信IP |
| 企业微信提示回调超时 | 开发机处理回调超过 5 秒；应保持回调只入库，下载放后台线程 |
| 消息重复处理 | 开发机按 `MsgId` 去重；企业微信超时会重试 |
| 开发机宕机 | 消息不会排队，企业微信重试 3 次后放弃；恢复后需手动补发 |

其他：

- 媒体文件会经过生产服务器中转，占用服务器带宽。
- 生产服务器在 proxy 模式下不写数据库、不落盘。
- 调试数据全部留在开发机的 `data-dev/`，不会污染生产。
