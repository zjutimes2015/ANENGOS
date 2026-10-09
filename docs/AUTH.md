# ANENGOS 登录与人机验证

## 能力（Better Auth 等价实现，Python 原生）

- **本地账号**：账号密码 + PBKDF2（sha256 / 120k 迭代）哈希，持久化于 `accounts.json`
- **会话**：HttpOnly Cookie（`anengos_session`，12 小时，SameSite=Lax），登出即销毁
- **人机验证**：PIL 算术图形验证码（点击刷新、单次消费、5 分钟过期），登录与 Web 注册强制校验
- **防爆破**：同一用户名+IP 连续失败 5 次锁定 15 分钟
- **鉴权链**：`Authorization: Bearer <token>` / `X-API-Token`（API 调用）→ Cookie 会话（Web 登录），二者兼容并存

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `ANENGOS_ADMIN_USER` | 否 | `admin` | 管理员登录用户名 |
| `ANENGOS_ADMIN_PASS` | 否 | 随机生成（不输出） | 管理员密码，**生产务必显式设置** |

> 管理员密码与 API 令牌互相独立：API 用 `ANENGOS_API_TOKEN`（Bearer），Web 控制台用账号密码登录。

## 端点

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/login` | 公开 | 登录页 |
| GET | `/api/captcha` | 公开 | 返回 `{captcha_id, image(dataURL)}` |
| POST | `/api/login` | 公开 | `{username, password, captcha_id, captcha_answer}` → `Set-Cookie` |
| POST | `/api/logout` | Cookie | 销毁会话 |
| POST | `/api/signup` | 公开 | 可选 `username/password` → 同时创建 Web 账号（`account_created`）；`captcha_id/answer` 必填时校验 |
| GET | `/admin` | 页面 | JS 检测 401 自动跳 `/login` |

## 页面

- `login.html`：深色登录页（验证码点击刷新，错误提示，失败自动换验证码）
- `admin.html`：未登录自动跳登录页；顶部新增「退出登录」按钮；令牌直填与 Cookie 会话并存
- `signup.html`：验证码 + 可选创建登录账号

## 生产部署要点

1. `accounts.json` 必须挂载持久化（部署命令见 `DEPLOYMENT_MANUAL.md`）：`-v /opt/anengos/accounts.json:/srv/anengos/accounts.json`
2. 服务器 `.env` 显式设置 `ANENGOS_ADMIN_USER` / `ANENGOS_ADMIN_PASS`（`chmod 600`），首次启动自动创建管理员
3. 镜像 tag 0.2.2+，`pip install pillow`

## 测试

`tests/test_auth.py`：验证码生成/消费、登录成功+会话鉴权、错误验证码/密码、5 次失败锁定、signup 带账号+验证码、登出销毁。全量 66 passed。
