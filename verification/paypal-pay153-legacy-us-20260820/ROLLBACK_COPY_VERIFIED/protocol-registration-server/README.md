# 服务器协议注册与优惠池

这是一个与本地网页、本地代理配置完全分离的服务器项目。它复用主项目已有的协议注册内核，使用 MVP、Repository 和 Strategy 组织批量任务、优惠分池与网络出口。

## 功能

- 网页设置 1–100 次注册，支持 1–10 并发、启动、停止和实时进度。
- 注册出口仅在服务器内按 `Clash 日本轮询 -> 服务器直连` 交替。
- 可切换为 Kookeey 注册代理，并为当前批次选择出口国家。
- 可选设置账号密码和 TOTP 2FA。
- 注册完成后立即使用保存的 Session 和注册线路验证账号。
- 可多选优惠检查国家；默认美国、英国、德国，并分别使用对应 Kookeey 出口创建带活动的 Checkout。
- 只有 Checkout 明确声明 PayPal 且应付最小货币单位金额为 `0` 才进入 `offer` 池；其余明确结果进入 `no_offer` 池，接口或代理结果不确定时进入待重试池。
- 当前国家 Checkout 没有 PayPal 或不可用时，自动切换 Kookeey DE 出口并改用 `DE/EUR` Checkout 账单复查，例如 BR、TH；这是上游“账单国家必须匹配请求国家”约束所需。
- 网页中每条优惠记录提供刷新图标，可使用 Kookeey 重新判断；已提交过的优惠 Checkout 会沿用，避免重复提交。
- 优惠池和本地导出会记录账号注册 IP、出口国家和代理模式。
- 最近任务参数和最终状态写入独立数据库，服务重启后仍可查看。
- 本地客户端通过 Bearer Token 调用 `/api/accounts?credentials=1` 获取池内账号、Session、Cookie、密码、TOTP 和优惠信息。

本项目不会写入本地代理配置。部署时的配置迁移只读源数据库，只复制 Kookeey 和 Zkgmail 两个允许的设置记录。

## 服务配置

复制 `.env.example` 为 `.env`，填写两个不少于 32 字符的令牌。服务器使用 host 网络访问现有服务：

- 独立 Clash Controller：`127.0.0.1:9099`
- 独立 Clash HTTP 代理：`127.0.0.1:7899`
- iCloud 验证码服务：`127.0.0.1:18767`
- 本服务：`127.0.0.1:18769`

服务器账号库默认挂载 `/opt/icloud-code-server/data` 到 `/shared-data`。独立优惠池数据库保存在 `./data/protocol-registration-server.db`。

`compose.yaml` 内的 `protocol-registration-clash` 只读取部署时复制到 `./clash/config.yaml` 的配置副本，不会连接或修改本地 Clash 配置文件。

## 配置迁移

以下命令只读源数据库并按键合并，不覆盖目标库中的其他表或设置：

```powershell
python -m protocol_registration_server.configuration `
  --source-db D:\AI\hidemyemail-generator\hidemyemail.db `
  --shared-db X:\server-copy\hidemyemail.db `
  --service-db X:\server-copy\protocol-registration-server.db
```

## 启动与验证

```bash
docker compose build
docker compose up -d
curl -fsS http://127.0.0.1:18769/healthz
curl -fsS -H "Authorization: Bearer $PROTOCOL_SERVER_API_TOKEN" \
  http://127.0.0.1:18769/api/status
```

公网入口片段位于 `Caddyfile`，默认地址为 `https://protocol-register.8-208-13-52.sslip.io`。

## 本地获取账号

```powershell
$env:PROTOCOL_SERVER_API_TOKEN = "服务器令牌"
.\scripts\Get-ServerAccounts.ps1 -Pool offer
.\scripts\Get-ServerAccounts.ps1 -Pool no_offer -OutputPath .\output\no-offer-accounts.json
```

`Pool` 可选 `all`、`offer`、`no_offer`、`pending`。不提供 `OutputPath` 时只输出 JSON。
