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

- 已配置可用的 SSH 主机别名 `cac`；
- 当前 Windows 用户环境中已设置至少 32 位的
  `HIDEMYEMAIL_REMOTE_TOKEN`；
- 项目根目录已有可用的 `inbox_config.json`、`cookies.txt` 和（可选）
  `hidemyemail.db`；
- 服务器已安装 Docker 和 Docker Compose。

运行：

```powershell
.\scripts\deploy-icloud-code-server.ps1
```

脚本将源码和运行数据上传到 `/opt/icloud-code-server`，生成仅服务器使用的 `.env`，
构建 `icloud-code-server` 容器，并检查 `/healthz`。密码、Cookie 和共享令牌不会输出
到控制台。若服务器已有最新运行数据，可加 `-SkipLocalData`，避免覆盖它们。

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

三个接口均使用请求头：

```text
X-HME-Import-Token: <共享令牌>
```

接口：

```text
GET  /api/integrations/registration-inventory/status
POST /api/integrations/registration-inventory/lease
POST /api/integrations/registration-inventory/result
```

领取成功返回 `leaseId`、`email` 和 `expiresAt`。注册结束后必须提交结果：

```json
{
  "leaseId": "领取时返回的 ID",
  "email": "alias@icloud.com",
  "success": true,
  "message": "OpenAI 注册成功"
}
```

- `success=true`：结束租约并把邮箱标记为 `used`；
- `success=false`：记录注册失败，把邮箱标记为 `trash`，永久退出自动库存；
- 10 分钟内没有回执：后台自动记为 `expired` 和 `trash`，不会再次分配。

库存使用“一次性领取”策略：只要某个邮箱进入过注册流程，无论最终成功、
失败、取消还是超时，都不会再被“从后台库存注册账号”领取。

本地服务需要配置：

```text
HIDEMYEMAIL_INVENTORY_URL=https://icloud-code.8-208-13-52.sslip.io
HIDEMYEMAIL_INVENTORY_TOKEN=<共享令牌>
```

`HIDEMYEMAIL_REMOTE_TOKEN` 只供部署和手动访问远端验证码服务使用；
`HIDEMYEMAIL_INVENTORY_TOKEN` 只供本地注册程序访问远端库存使用。它们当前
可以使用同一个值，但不得再使用全局 `HME_IMPORT_TOKEN`，以免覆盖本地
工作台导入令牌。

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
