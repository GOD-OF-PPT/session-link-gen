# Session Link Generator

从 ChatGPT Session (Access Token) 生成 ChatGPT Plus 支付长链接的独立服务端项目。

从桌面版 `app.py` 提取而来，去除所有 Tkinter GUI 依赖，可部署在任何服务器上。

## 安装

```bash
cd session-link-gen
python -m venv .venv
source .venv/bin/activate    # Linux/Mac
# .venv\Scripts\activate     # Windows

pip install -r requirements.txt
```

## 启动

```bash
# 开发模式
python app.py

# 生产模式 (Windows)
pip install waitress
waitress-serve app:app

# 生产模式 (Linux)
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:5000 app:app
```

## Linux 服务器部署（IP:端口访问）

上传项目到服务器后，在项目目录执行：

```bash
chmod +x deploy_linux.sh
./deploy_linux.sh
```

默认访问地址：

```bash
http://服务器IP:5000
```

自定义端口或重试次数：

```bash
PORT=8080 GENERATE_RETRIES=20 ./deploy_linux.sh
```

查看服务状态和日志：

```bash
sudo systemctl status session-link-gen
sudo journalctl -u session-link-gen -f
```

如果外部无法访问，检查云服务器安全组和系统防火墙是否放行对应端口，例如：

```bash
sudo ufw allow 5000/tcp
```

## 代理配置

通过环境变量设置默认代理：

```bash
# 单个代理
set PROXY_DEFAULT_URL=http://127.0.0.1:7890

# 或者请求时传入 proxy 参数
```

也支持通过 `ProxyChainServer` 链式代理（本地代理 → 动态代理池 → 目标）。

## 自动重试

生成支付长链接默认自动重试 20 次。可通过环境变量调整：

```bash
set GENERATE_RETRIES=20
set GENERATE_RETRY_DELAY=0.5
```

接口请求也可传入 `retry_count` 覆盖本次请求的重试次数。

## API 接口

### `GET /api/health`

健康检查。

```bash
curl http://127.0.0.1:5000/api/health
# {"ok": true, "time": "2026-06-22T..."}
```

### `GET /api/payment-modes`

列出所有支持的支付模式。

```bash
curl http://127.0.0.1:5000/api/payment-modes
```

返回的支付模式：

| 模式名 | 类型 | country | currency |
|--------|------|---------|----------|
| 无卡长链接 US/USD | hosted | US | USD |
| 无卡长链接 BR/BRL | hosted | BR | BRL |
| 无卡长链接 DE/EUR | hosted | DE | EUR |
| 无卡长链接 FR/EUR | hosted | FR | EUR |
| 无卡长链接 GB/GBP | hosted | GB | GBP |
| 无卡长链接 CA/CAD | hosted | CA | CAD |
| 无卡长链接 AU/AUD | hosted | AU | AUD |
| 无卡长链接 JP/JPY | hosted | JP | JPY |
| GoPay 长链接 ID/IDR | hosted | ID | IDR |
| **PayPal 长链接 US/USD** | paypal | US | USD |
| **PayPal 长链接 FR/EUR** | paypal | FR | EUR |
| Apple Pay 支付页 US/USD | hosted | US | USD |
| Apple Pay 支付页 JP/JPY | hosted | JP | JPY |

### `POST /api/generate-link`

用 Access Token 生成长链接。

```bash
curl -X POST http://127.0.0.1:5000/api/generate-link \
  -H "Content-Type: application/json" \
  -d '{
    "access_token": "eyJhbG...your-access-token...",
    "payment_mode": "无卡长链接 US/USD",
    "proxy": "http://127.0.0.1:7890"
  }'
```

响应：
```json
{
  "ok": true,
  "long_url": "https://checkout.stripe.com/c/pay/cs_xxx...",
  "payment_mode": "无卡长链接 US/USD",
  "result": { ... }
}
```

### `POST /api/paste-session`

粘贴完整 Session JSON，自动解析 Access Token 并生成长链接。

```bash
curl -X POST http://127.0.0.1:5000/api/paste-session \
  -H "Content-Type: application/json" \
  -d '{
    "session_json": "{\"accessToken\": \"eyJhbG...\", \"user\": {...}}",
    "payment_mode": "PayPal 长链接 US/USD"
  }'
```

响应：
```json
{
  "ok": true,
  "access_token": "eyJhbG...",
  "long_url": "https://www.paypal.com/agreements/approve?ba_token=...",
  "payment_mode": "PayPal 长链接 US/USD",
  "result": { ... }
}
```

## 支付模式说明

- **无卡长链接** / **GoPay 长链接** / **Apple Pay 支付页**：
  生成 Stripe hosted checkout URL，直接返回支付页面链接。

- **PayPal 长链接**：
  完整走 Stripe payment_methods → confirm → extract PayPal BA approve URL，
  返回 `https://www.paypal.com/agreements/approve?ba_token=...` 格式的链接。

## 核心模块

`core.py` 包含所有纯函数，无需 Flask 即可直接使用：

```python
from core import parse_session_json, generate_payment_link

access_token = parse_session_json(session_text)
result = generate_payment_link(access_token, "无卡长链接 US/USD", proxy_url)
print(result["long_url"])
```

## 项目结构

```
session-link-gen/
├── core.py              # 核心逻辑 (所有函数提取自原 app.py)
├── app.py               # Flask HTTP API 服务器
├── requirements.txt     # 依赖
└── README.md
```
