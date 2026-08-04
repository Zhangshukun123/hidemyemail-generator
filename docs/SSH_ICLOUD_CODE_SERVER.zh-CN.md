# 通过 SSH 使用服务器 iCloud 验证码服务

服务器程序基于本项目已有的 IMAP 收件箱和 OpenAI 验证码解析逻辑。它通过
`imap.mail.me.com:993` 的 SSL IMAP 读取转发到主 iCloud 邮箱的邮件，按目标
`@icloud.com` 隐藏邮箱匹配验证码，并把结果保存在服务器本地 SQLite 数据库中。

程序容器只监听服务器的 `127.0.0.1:18767`。服务器 Caddy 通过独立 HTTPS 域名只
反向代理健康检查和取码 API，请求仍必须携带共享令牌。管理页面和其他接口不会开放。

正式服务地址：

```text
https://icloud-code.8-208-13-52.sslip.io
```

## 部署

本机需要：

- 已配置可用的 SSH 主机别名 `aliyun-ecs`；
- 当前 Windows 用户环境中已设置至少 32 位的 `HME_IMPORT_TOKEN`；
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
