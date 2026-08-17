# zkgmail.com 验证码接收站

这是一个独立、只读的公开接码门户。它按 MVP 分层，并通过 Repository、Adapter、Strategy、Decorator 模式隔离页面、业务逻辑和 QQ IMAP。

## 结构

- `domain.py`：Model 与输入约束。
- `presenter.py`：Presenter，映射查询结果与错误。
- `view.py`、`static/`：View 与浏览器交互。
- `adapters/imap_repository.py`：QQ IMAP Adapter、Repository 与缓存 Decorator。
- `strategies/keyword_code_extractor.py`：可替换的验证码提取 Strategy。
- `invite.py`、`access_session.py`：精确邮箱邀请与不透明访问会话。
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

为一个具体邮箱生成仅在 URL fragment 中携带令牌的邀请链接：

```powershell
$env:ZKGMAIL_ACCESS_TOKEN = "<与服务器相同的 64 位十六进制密钥>"
zkgmail-code-invite person@zkgmail.com --hours 168
```

每个邀请只允许查询该链接绑定的完整邮箱；浏览器交换令牌后使用 Secure、HttpOnly、
SameSite=Strict 的不透明会话 Cookie。服务端限制每个邀请、全局会话及 IP/会话/邮箱
查询频率。

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

将 `Caddyfile` 站点块合并到现有 Caddy 配置，验证后热重载。Web 解析变更不应修改域名已有的 MX/TXT 邮件记录。
