# ANENGOS 私有化部署手册（客户交付版）

> 版本：v0.3.0 ｜ 适用镜像：`anengos:0.2.0` ｜ 更新日期：2026-10-09
> 面向：已采购 ANENGOS 私有化授权的客户 IT 负责人 / 运维工程师

---

## 1. 交付物清单

| 交付物 | 说明 |
|---|---|
| 镜像 `anengos:0.2.0`（或源码包 `ANENGOS-<版本>.tar.gz`） | Docker 镜像或完整源码，二选一 |
| 本部署手册 | 本文档 |
| `.env` 配置模板 | 模型 key / 管理员令牌等 |
| 管理台访问说明 | `http://<服务器IP>:8080` |
| 培训（可选） | 1 次线上培训，2 小时 |

## 2. 服务器最低要求

| 项目 | 最低配置 | 推荐配置 |
|---|---|---|
| CPU | 2 核 | 4 核 |
| 内存 | 2 GB 可用 | 4 GB 可用 |
| 磁盘 | 10 GB 可用 | 50 GB（工作区 + 审计增长） |
| 操作系统 | Ubuntu 20.04+ / Debian 11+ / CentOS 7+ | Ubuntu 24.04 LTS |
| 网络 | 可访问模型 API（DeepSeek / 豆包 / OpenAI 兼容） | 同左 + 公网 80/443 入站 |

> 模型推理在模型厂商侧完成，本服务器只运行治理层（审批 / 审计 / 互审 / 任务调度），不承载推理负载。

## 3. 安装步骤

### 3.1 安装 Docker（如未安装）

```bash
# Ubuntu / Debian
curl -fsSL https://get.docker.com | sh
sudo systemctl enable --now docker
```

### 3.2 准备配置文件

创建 `/opt/anengos` 目录并写入 `.env`：

```bash
sudo mkdir -p /opt/anengos/{workspace,audit}
sudo tee /opt/anengos/.env > /dev/null <<'EOF'
# ===== 必填 =====
ANENGOS_API_KEY=sk-xxxx                 # 模型 API Key（DeepSeek / 豆包 / 其他 OpenAI 兼容）
ANENGOS_BASE_URL=https://api.deepseek.com/v1
ANENGOS_MODEL=deepseek-chat
ANENGOS_API_TOKEN=<生成一个强随机令牌>   # 管理员令牌：openssl rand -hex 24
# ===== 可选 =====
ANENGOS_PORT=8080
# 外部智能体适配器（BYOK，可选）：
# ANENGOS_CODEX_MODE=mock               # cli | http | mock
# ANENGOS_DOUBAO_API_KEY=...
# ANENGOS_GROK_API_KEY=...
EOF
```

### 3.3 构建并启动

```bash
cd /opt/anengos
# 方式 A：已有镜像
sudo docker run -d --name anengos --restart unless-stopped \
  -p 8080:8080 \
  --env-file /opt/anengos/.env \
  -v /opt/anengos/workspace:/srv/anengos/workspace \
  -v /opt/anengos/audit:/srv/anengos/audit \
  -v /opt/anengos/tenants.json:/srv/anengos/tenants.json \
  anengos:0.2.0

# 方式 B：源码构建
# 解压源码包后：
sudo docker build -t anengos:0.2.0 .
sudo docker run -d ...（同上）
```

### 3.4 健康检查

```bash
curl http://127.0.0.1:8080/health
# 期望：{"status":"ok","service":"anengos","has_api_key":true,"auth_required":true,...}
```

## 4. 使用入口

| 入口 | 地址 | 说明 |
|---|---|---|
| 管理台 | `http://<IP>:8080/` | 任务 / 审批 / 互审 / 审计 / 用量 / 租户管理 |
| 自助开通 | `http://<IP>:8080/signup` | 客户自助注册试用租户（可关闭） |
| API | `POST /run`、`POST /admin/api/run-async` 等 | 需 `Authorization: Bearer <token>` |
| 健康检查 | `GET /health` | Docker healthcheck 已内置 |

## 5. 多租户管理（客户开通）

1. 管理台「客户账号」面板，或 `POST /admin/api/tenants {"name":"客户名"}` 创建租户
2. 系统返回一次性明文 token → 立即转交客户
3. 客户用该 token 调用 API / 登录管理台（数据完全隔离：工作区 / 审批队列 / 审计）
4. 用量配额：`POST /admin/api/tenants/{id}/quota {"tasks_per_month":500}` 调整套餐
5. 停用客户：`POST /admin/api/tenants/{id}/revoke`

## 6. 备份与恢复

```bash
# 备份（工作区 + 审计 + 租户表）
sudo tar czf anengos_backup_$(date +%F).tgz \
  /opt/anengos/workspace /opt/anengos/audit /opt/anengos/tenants.json
# 恢复：解压回 /opt/anengos 后重启容器
sudo docker restart anengos
```

> 任务与审批队列保存在内存（容器重建即清空），属运行态数据；业务数据（工作区文件 / 审计 / 租户表）均在挂载卷中。

## 7. 升级

```bash
cd /opt/anengos
sudo docker build -t anengos:0.3.0 .   # 或拉取新镜像
sudo docker stop anengos && sudo docker rm anengos
sudo docker run -d ...（同上，镜像名换新版本）
```

## 8. 安全清单

- [ ] `ANENGOS_API_TOKEN` 使用强随机值，且仅存于 `.env`（权限 600）
- [ ] 管理台 / API 全部走鉴权；公开端点仅 `/health` 与 `/signup`
- [ ] 生产环境务必用 HTTPS（Nginx 反代 + 证书），不要裸 HTTP 暴露公网
- [ ] `.env` 含模型 key，禁止提交到 Git / 外发
- [ ] 定期备份挂载卷；审计日志至少保留 180 天（合规需要）
- [ ] 若关闭自助开通：Nginx 层屏蔽 `/signup` 或在防火墙限制

## 9. 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `/health` 返回 `has_api_key:false` | `.env` 未加载 / key 缺失 | 检查 `--env-file` 路径与 key |
| 任务报「模型 API 返回 400」 | 模型 key 无效或余额不足 | 检查 key / 账户余额 |
| 管理台 401 | token 错误 | 用管理员 token 重试 |
| 容器反复重启 | 磁盘满 / 端口占用 | `docker logs anengos` 查看 |
| 审批后任务 error | 模型未跟随 tool 结果 | 属模型行为，重试或换模型 |

---

# ANENGOS 报价单

## 1. 产品与授权范围

- 产品：ANENGOS AI 生产力操作系统（治理型多智能体平台）
- 授权方式：永久使用授权（绑定客户服务器）+ 年度维保
- 授权范围：单台服务器，租户数不限（按套餐上限）
- 交付物：镜像 / 源码 + 部署手册 + 管理台 + 多租户 + 审计互审全套功能

## 2. 价格表（人民币，含 6% 增值税）

| 档位 | 授权费（一次性） | 年维保（授权费 15%） | 适用客户 |
|---|---|---|---|
| 标准版 | ¥58,000 | ¥8,700/年 | ≤10 租户、≤3 外部智能体、无定制 |
| 企业版 | ¥128,000 | ¥19,200/年 | 租户不限、全部适配器、SLA 99.5%、专属支持 |
| 旗舰版 | ¥288,000 起 | 面议 | 深度定制（对接内部系统 / 专属适配器 / 模型微调配合） |

> 部署实施费：标准版已含 1 次远程实施（1 人天）；现场实施 ¥2,000/人天。

## 3. SaaS 订阅价（可选，云托管）

| 档位 | 价格 | 说明 |
|---|---|---|
| 试用 | ¥0 | 100 任务/月、1 租户、1 适配器 |
| 团队版 | ¥299/月（年付 ¥2,999） | 5 席位、2 适配器、挂起续跑 |
| 企业 SaaS | ¥1,500/月（年付 ¥15,000） | 多租户、审计导出、互审面板、SLA |

## 4. 计费口径

- 任务 = 一次 `POST /run` 或 `run-async` 提交（含挂起后完成的同一任务，只计 1 次）
- 配额按月滚动：每月 1 日 UTC 重置
- 模型调用费用由客户自理（BYOK：客户自带 DeepSeek / 豆包 / Grok key）
- 超出配额不产生费用，仅暂停新任务，提示升级

## 5. 商务条款

- 付款：首付 60%（签约）+ 尾款 40%（验收后 10 日内）
- 验收标准：部署完成 + 管理台可登录 + 一次端到端任务（提交→审批→审计→互审）跑通
- 维保范围：版本升级、缺陷修复、远程支持（工作时间 4 小时响应）
- 终止：客户欠费或违反授权条款，授权暂停；数据归客户所有，可自行导出

## 6. 报价单模板（填写后生效）

```
报价编号：ANENGOS-2026-____
客户名称：____________  联系人：____________  电话：____________
套餐：□ 标准版 ¥58,000   □ 企业版 ¥128,000   □ 旗舰版 ¥288,000 起
增项：□ SaaS 订阅（____档 × ____月）  □ 现场实施（____人天 × ¥2,000）
       □ 培训（____次 × ¥3,000）     □ 其他定制：____________
合计金额（含税）：¥____________  大写：____________
付款方式：签约付 60% ¥____ / 验收付 40% ¥____
有效期：本报价自出具之日起 30 日内有效
报价人：____________  日期：____________
```
