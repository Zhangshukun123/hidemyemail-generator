# zkgmail.com 验证码接收站

这是一个独立、只读的公开接码门户。它按 MVP 分层，并通过 Repository、Adapter、Strategy、Decorator 模式隔离页面、业务逻辑和 QQ IMAP。

## 结构

- `domain.py`：Model 与输入约束。
- `presenter.py`：Presenter，映射查询结果与错误。
- `view.py`、`static/`：View 与浏览器交互。
- `adapters/imap_repository.py`：QQ IMAP Adapter、Repository 与缓存 Decorator。
- `strategies/keyword_code_extractor.py`：可替换的验证码提取 Strategy。
- `invite.py`、`access_session.py`：精确邮箱邀请与可跨容器重启恢复的不透明访问会话。
- `app.py`：依赖装配与薄 HTTP 路由。

## 本地运行

复制 `.env.example` 为 `.env`，填写 IMAP 用户名和授权码，然后执行：

```powershell
python -m pip install -e .
zkgmail-code-server --host 127.0.0.1 --port 18768
```

生产环境必须将 `ZKGMAIL_ACCESS_TOKEN` 设置为 64 位十六进制随机密钥，并将
`ZKGMAIL_TRUSTED_RECIPIENT_HEADER` 设置为转发服务在信封阶段写入且会覆盖的唯一原始
收件人头。普通 `To/Cc` 与正文不会参与邮箱匹配。

默认 `ZKGMAIL_REQUIRE_INVITE=true`。如果需要兼容已经公开运行、允许直接输入地址的
旧站点，可显式设置 `ZKGMAIL_REQUIRE_INVITE=false`；该模式不会启用邀请 Cookie，安全
边界与旧版相同，不应作为新部署的默认值。

为一个具体邮箱生成仅在 URL fragment 中携带令牌的邀请链接：

```powershell
$env:ZKGMAIL_ACCESS_TOKEN = "<与服务器相同的 64 位十六进制密钥>"
zkgmail-code-invite person@zkgmail.com --hours 168
```

每个邀请只允许查询该链接绑定的完整邮箱；浏览器交换令牌后使用 Secure、HttpOnly、
SameSite=Strict 的不透明会话 Cookie。服务端限制每个邀请、全局会话及 IP/会话/邮箱
查询频率。Docker 部署把会话令牌的 SHA-256 摘要写入专用数据卷，不保存 Cookie
原文；因此服务器或容器重启后，尚未过期的浏览器会话仍可继续接码。

测试：

```powershell
python -m pytest -q
python -m ruff check .
```

## 部署

```bash
docker compose config -q
docker compose up -d --build
curl --fail http://127.0.0.1:18768/healthz
```

生产服务器安装随仓库提供的 systemd 单元，让开机流程在网络和 Docker 就绪后重新
校验 Compose 配置、拉起正确镜像并等待健康检查：

```bash
install -m 0644 zkgmail-code-server.service /etc/systemd/system/zkgmail-code-server.service
systemctl daemon-reload
systemctl enable --now zkgmail-code-server.service
```

`docker compose down` 默认不会删除 `zkgmail-session-data` 卷；只有显式使用 `-v`
才会移除持久会话。网页遇到 Caddy/Docker 重启造成的短暂断线时，会在三分钟查询
窗口内自动重连。

将 `Caddyfile` 站点块合并到现有 Caddy 配置，验证后热重载。Web 解析变更不应修改域名已有的 MX/TXT 邮件记录。
