# 注册浏览器架构与故障路由

浏览器注册代码按职责拆成独立模块。修复问题时先根据诊断码和故障阶段进入对应模块，避免继续向 `openai_browser_bridge.py` 堆叠页面细节。

| 模块 | 负责内容 |
| --- | --- |
| `browser_diagnostics.py` | 稳定诊断码、UI 阶段映射 |
| `browser_platform.py` | Windows 剪贴板、窗口尺寸、聚焦与并发平铺 |
| `registration_auth.py` | 首页登录/注册按钮、邮箱聚焦与剪贴板粘贴、登录/继续提交 |
| `openai_browser_selectors.py` | 账号设置、密码、验证码等选择器及浏览器端脚本 |
| `openai_bridge_runtime.py` | Session 状态读取、日志脱敏、Camoufox 运行目录、输入回读 |
| `openai_browser_dom.py` | 通用 DOM 查找、点击、菜单、密码行和页面等待 |
| `openai_registration_flow.py` | 注册 Worker 补丁、安全挑战、基础资料、邮箱密码路径 |
| `openai_registration_navigation.py` | 首页入口、认证页资源检查、直连和容错导航 |
| `openai_registration_otp.py` | 自动取码、本地验证码接口、浏览器手动验证码 |
| `openai_account_security.py` | 注册后 Session、密码设置、账号安全页和 TOTP 2FA |
| `openai_browser_cli.py` | 单次浏览器任务的命令行执行与结果编排 |
| `openai_browser_bridge.py` | 兼容导出和依赖注入，不实现页面细节或任务流程 |

## 快速定位

| 现象或诊断码 | 首查模块 |
| --- | --- |
| `WINDOW_SINGLE_STABLE`、`WINDOW_TILING_ENABLED`、启动闪屏或窗口重叠 | `browser_platform.py` |
| `AUTH_HOME_READY`、`AUTH_HOME_LOGIN_CLICK`、`AUTH_HOME_LOGIN_RETRY` | `registration_auth.py`、`openai_registration_navigation.py` |
| `AUTH_DIRECT_NAV_BLOCKED`、意外生成或跳转认证 URL | `openai_registration_navigation.py` |
| `AUTH_EMAIL_FOCUS`、`AUTH_EMAIL_PASTE`、`AUTH_EMAIL_SUBMIT` | `registration_auth.py` |
| Cloudflare 或手动安全验证后不继续 | `openai_registration_flow.py` |
| 基础资料填写、回读或提交异常 | `openai_registration_flow.py` |
| 密码设置、账号菜单或安全页异常 | `openai_account_security.py`、`openai_browser_dom.py` |
| 自动取码或手动验证码卡住 | `openai_registration_otp.py` |
| Session、日志脱敏或 Camoufox 运行目录异常 | `openai_bridge_runtime.py` |

## 修改边界

1. 新增页面语言或选择器时，修改选择器所属模块及对应测试。
2. Windows API、窗口和剪贴板问题只修改 `browser_platform.py`。
3. 新诊断码必须在 `browser_diagnostics.py` 注册 UI 上下文。
4. `openai_browser_bridge.py` 只保留兼容包装；新业务逻辑不得直接加入该文件。
5. 兼容包装在调用时注入依赖，旧代码仍可补丁 bridge 中的剪贴板、窗口聚焦、MFA 客户端等入口。
6. 验证顺序：模块测试、`tests.test_browser_tasks`、完整 `unittest` 回归，再在任务空闲时重启服务。
