# 通过 SSH 使用服务器 iCloud 验证码服务

服务器程序基于本项目已有的 IMAP 收件箱和 OpenAI 验证码解析逻辑。它通过
`imap.mail.me.com:993` 的 SSL IMAP 读取转发到主 iCloud 邮箱的邮件，按目标
`@icloud.com` 隐藏邮箱匹配验证码，并把结果保存在服务器本地 SQLite 数据库中。

收件采用按需模式：服务器空闲时不会连接或登录 IMAP；只有取码接口收到请求且本地
数据库没有对应验证码时才同步邮箱。并发请求共享一次同步，成功后有短暂冷却时间，
登录失败则自动延长重试间隔，避免持续认证触发邮箱服务商风控。

程序容器只监听服务器的 `127.0.0.1:18767`。服务器 Caddy 通过独立 HTTPS 域名只
反向代理登录页、接码页、健康检查、取码 API 和注册邮箱库存 API。
其他管理页面和接口不会开放。

正式服务地址：

```text
https://icloud-code.8-208-13-52.sslip.io
```

固定接码页入口（可直接收藏，无需登录）：

```text
https://icloud-code.8-208-13-52.sslip.io/code
```

该页面只提供“输入 iCloud 隐藏邮箱→读取 OpenAI 验证码”功能。
账号管理、邮箱库存和其他后台接口仍需要认证。

## 部署

本机需要：

- 已配置可用的 SSH 主机别名 `aliyun-ecs`；
- 当前 Windows 用户环境中已设置至少 32 位的
  `HIDEMYEMAIL_REMOTE_TOKEN`；
- 当前 Windows 用户环境中已设置 `HIDEMYEMAIL_INVENTORY_USERNAME` 和
  `HIDEMYEMAIL_INVENTORY_PASSWORD`；
- 项目根目录已有可用的 `inbox_config.json`、`cookies.txt` 和（可选）
  `hidemyemail.db`；
- 服务器已安装 Docker 和 Docker Compose。

运行：

```powershell
.\scripts\deploy-icloud-code-server.ps1
```

脚本将源码和运行数据上传到 `/opt/icloud-code-server`，生成仅服务器使用的 `.env`，
构建 `icloud-code-server` 容器，并检查 `/healthz`。密码、Cookie 和共享令牌不会输出
到控制台。首次启动把账号和密码转换成 SQLite 中的 `scrypt` 加盐哈希后，脚本会从
服务器 `.env` 删除明文登录凭据并重新创建容器。若服务器已有最新运行数据，可加
`-SkipLocalData`，避免覆盖它们。

## HTTPS 取码接口

接口地址：

```text
POST https://icloud-code.8-208-13-52.sslip.io/api/integrations/workbench/openai-code
```

`openai-register-paylink` 的 `HME_SERVICE_URL` 应设置为：

```text
https://icloud-code.8-208-13-52.sslip.io
```

服务器运行所需的 IMAP 配置、Cookie、共享令牌和 SQLite 数据库均保存在
`/opt/icloud-code-server`，运行时不依赖部署它的 Windows 电脑。

## 注册邮箱库存接口

后台服务在每个整点尝试生成 5 个 iCloud 隐藏邮箱并存入服务器 SQLite 数据库。本地
注册程序不再自行定时生成；每次注册都通过 HTTPS 领取一个邮箱租约。租约默认锁定
10 分钟，同一邮箱不会同时分配给两个注册任务。

先使用账号密码登录：

```text
POST /api/integrations/registration-inventory/login
{"username":"<账号>","password":"<密码>"}
```

登录成功返回 12 小时临时 `accessToken`。其余接口使用请求头
`Authorization: Bearer <accessToken>`；服务重启或令牌过期后客户端会自动重新登录。
服务器 SQLite 只保存 `scrypt` 加盐密码哈希，不保存明文密码。

库存接口：

```text
GET  /api/integrations/registration-inventory/status
POST /api/integrations/registration-inventory/lease
POST /api/integrations/registration-inventory/sync
POST /api/integrations/registration-inventory/result
```

本地服务启动后会通过 `sync` 分批上传现有邮箱，包含 `addresses` 表的全部字段
（邮箱、标签、状态、来源、备注、启用状态、批次和创建/更新时间），以及
`gpt_account:<email>` 中的完整账号对象。之后默认每 5 分钟补偿同步一次；浏览器注册和
协议注册保存账号后还会立即同步一次。同步只新增或合并记录，不会因为某台客户端缺少
记录而删除服务器已有数据。

领取成功返回 `leaseId`、`email`、`expiresAt` 和完整 `record`。本地会先把 `record`
写入 SQLite，再开始注册。注册结束后必须提交结果；成功回执同时携带最新完整账号记录：

```json
{
  "leaseId": "领取时返回的 ID",
  "email": "alias@icloud.com",
  "success": true,
  "message": "OpenAI 注册成功",
  "record": {
    "email": "alias@icloud.com",
    "address": {"email": "alias@icloud.com", "state": "used"},
    "account": {"email": "alias@icloud.com", "session": {}}
  }
}
```

- `success=true`：结束租约并把邮箱标记为 `used`；
- `success=false`：记录注册失败，把邮箱释放回 `unused`，可再次分配；
- 10 分钟内没有回执：后台自动记为 `expired` 并释放回 `unused`。

库存只锁定正在注册的邮箱，并永久排除已经注册成功的邮箱；失败、取消或
超时的邮箱会回到可用库存，可再次被“从后台库存注册账号”领取。

本地服务需要配置：

```text
HIDEMYEMAIL_INVENTORY_URL=https://icloud-code.8-208-13-52.sslip.io
HIDEMYEMAIL_INVENTORY_USERNAME=<登录账号>
HIDEMYEMAIL_INVENTORY_PASSWORD=<登录密码>
HIDEMYEMAIL_INVENTORY_SYNC_INTERVAL_SECONDS=300
```

正式远端地址统一使用 HTTPS：如果省略协议会自动补上 `https://`，公网
`http://` 地址也会自动升级为 `https://`；只有 `localhost`、`127.0.0.1` 和 `::1`
测试地址保留 HTTP。HTTPS 在 DNS、TCP 或 TLS 建连阶段失败时会有限重试，已可能
送达服务器的请求不会自动重发。同步请求使用登录后得到的 Bearer 令牌，单批最多
50 条，并受 Caddy 请求体大小限制保护。

`HIDEMYEMAIL_REMOTE_TOKEN` 只供部署和手动访问远端验证码服务使用；
`HIDEMYEMAIL_INVENTORY_TOKEN` 只用于尚未配置账号密码的旧版服务器兼容模式。
配置 `HIDEMYEMAIL_INVENTORY_USERNAME` 和 `HIDEMYEMAIL_INVENTORY_PASSWORD` 后，
库存接口不再接受旧共享令牌；全局 `HME_IMPORT_TOKEN` 也不会作为库存认证使用。

## SSH 隧道（仅用于维护）

双击 `连接远程-iCloud子邮箱.cmd`。窗口需要保持打开。本机服务地址为：

```text
http://127.0.0.1:18765
```

正常取码无需开启该隧道；它只作为 HTTPS 或 Caddy 故障时的维护通道。

## 手工获取一次验证码

直接运行：

```powershell
.\获取iCloud验证码.ps1 target-alias@icloud.com
```

命令会等待本次启动后收到的 6 位 OpenAI 验证码，默认最多等待 120 秒，成功时只输出
验证码。可用 `-TimeoutSeconds 300` 延长等待时间。
