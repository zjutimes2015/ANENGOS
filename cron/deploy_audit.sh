#!/bin/bash
# ANENGOS 部署同步自检 hook
# 目的：防止"GitHub 已提交、服务器仍跑旧代码"的部署脱节。
# 比对三处代码一致性：GitHub main（基准） vs 主机构建上下文 vs 运行容器；
# 并检查飞书凭据、secrets、workspace 可写、语法编译与功能标志。
#
# 用法：
#   /opt/anengos/cron/deploy_audit.sh   # 完整自检，结果写 audit 日志
# 退出码：0=全部通过；1=存在 FAIL 项（WARN 不影响退出码）。
# 说明：本脚本为 fail-open 告警型前置检查，即便自检自身异常也不应阻断当日策略运行。

set -u

REPO_RAW="https://raw.githubusercontent.com/zjutimes2015/ANENGOS/main/connectors"
CONTAINER="anengos"
CTR_DIR="/srv/anengos/connectors"
HOST_DIR="/opt/anengos/connectors"
WORKSPACE="/opt/anengos/workspace"
SECRETS="$WORKSPACE/.secrets/feishu_app.json"
AUDIT_DIR="/opt/anengos/audit"
AUDIT_LOG="$AUDIT_DIR/deploy_audit.log"
TMP_DIR="$(mktemp -d /tmp/anengos_audit.XXXXXX)"

FILES=("paper_trader.py" "quant_ersibang.py" "push_daily_review.py")
# 功能标志：文件名 -> 该文件必须出现的关键字（逗号分隔，全部命中才算 PASS）
declare -A FLAGS=(
  ["paper_trader.py"]="_sync_pending_to_feishu,esb_pending_signal,feishu_app.json,paper_pending_orders.json"
  ["quant_ersibang.py"]="esb_pending_signal"
  ["push_daily_review.py"]="tenant_access_token"
)

FAIL=0
WARN=0
PASS=0

mkdir -p "$AUDIT_DIR"
exec 9>>"$AUDIT_LOG"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee /dev/fd/9; }
ok()   { PASS=$((PASS+1)); log "[PASS] $*"; }
bad()  { FAIL=$((FAIL+1)); log "[FAIL] $*"; }
warn() { WARN=$((WARN+1)); log "[WARN] $*"; }

cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT

# 1. 容器健康状态
log "==== ANENGOS 部署自检开始 ===="
health="$(sudo docker inspect --format '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo "unknown")"
if [ "$health" = "healthy" ]; then
  ok "容器 $CONTAINER 健康状态: $health"
else
  bad "容器 $CONTAINER 健康状态: ${health:-取不到}"
fi

# 2. 拉取 GitHub main 基准文件并算 md5
for f in "${FILES[@]}"; do
  if ! curl -sS -m 20 -o "$TMP_DIR/$f" "$REPO_RAW/$f"; then
    bad "GitHub 基准下载失败: $f"
    continue
  fi
  base_md5="$(md5sum "$TMP_DIR/$f" | awk '{print $1}')"

  # 主机上下文
  if [ -f "$HOST_DIR/$f" ]; then
    host_md5="$(md5sum "$HOST_DIR/$f" | awk '{print $1}')"
    [ "$host_md5" = "$base_md5" ] && ok "主机上下文 $f 与 main 一致 ($base_md5)" \
      || bad "主机上下文 $f 与 main 不一致: host=$host_md5 main=$base_md5"
  else
    bad "主机上下文缺少文件: $HOST_DIR/$f"
  fi

  # 运行容器
  ctr_md5="$(sudo docker exec "$CONTAINER" md5sum "$CTR_DIR/$f" 2>/dev/null | awk '{print $1}')"
  if [ -n "$ctr_md5" ]; then
    [ "$ctr_md5" = "$base_md5" ] && ok "容器内 $f 与 main 一致 ($ctr_md5)" \
      || bad "容器内 $f 与 main 不一致: ctr=$ctr_md5 main=$base_md5"
  else
    bad "容器内缺少文件: $CTR_DIR/$f"
  fi

  # 语法编译（以容器内 python 为准）
  if sudo docker exec "$CONTAINER" python3 -m py_compile "$CTR_DIR/$f" 2>/dev/null; then
    ok "容器内 $f 语法编译通过"
  else
    bad "容器内 $f 语法编译失败"
  fi

  # 功能标志
  IFS=',' read -ra keys <<< "${FLAGS[$f]:-}"
  for k in "${keys[@]}"; do
    if sudo docker exec "$CONTAINER" grep -qF "$k" "$CTR_DIR/$f" 2>/dev/null; then
      ok "$f 含功能标志: $k"
    else
      bad "$f 缺少功能标志: $k"
    fi
  done
done

# 3. 飞书凭据 secrets
CTR_SECRETS="/srv/anengos/workspace/.secrets/feishu_app.json"
if [ -f "$SECRETS" ]; then
  app_id="$(sudo docker exec "$CONTAINER" python3 -c "import json;print(json.load(open('$CTR_SECRETS')).get('app_id',''))" 2>/dev/null)"
  app_secret_set="$(sudo docker exec "$CONTAINER" python3 -c "import json;print('yes' if json.load(open('$CTR_SECRETS')).get('app_secret','') else 'no')" 2>/dev/null)"
  [ -n "$app_id" ] && ok "secrets.app_id 已配置（不回显值）" || bad "secrets.app_id 为空"
  [ "$app_secret_set" = "yes" ] && ok "secrets.app_secret 已配置（不回显值）" || bad "secrets.app_secret 为空"
else
  bad "凭据文件缺失: $SECRETS"
fi

# 4. workspace 可写 & 挂单持久化文件状态
if sudo docker exec "$CONTAINER" sh -c "test -w /srv/anengos/workspace" 2>/dev/null; then
  ok "workspace 挂载卷可写"
else
  bad "workspace 挂载卷不可写"
fi
if sudo docker exec "$CONTAINER" test -f /srv/anengos/workspace/paper_pending_orders.json 2>/dev/null; then
  ok "挂单持久化文件存在: paper_pending_orders.json"
else
  warn "挂单持久化文件尚不存在（首次运行或当日无挂单属正常）"
fi

# 5. 汇总
log "==== 自检汇总: PASS=$PASS WARN=$WARN FAIL=$FAIL ===="
if [ "$FAIL" -gt 0 ]; then
  log "[DEPLOY_AUDIT RESULT] FAIL —— 存在代码脱节或配置缺失，请先对齐 GitHub main 再运行策略"
  exit 1
fi
log "[DEPLOY_AUDIT RESULT] OK —— 服务器代码与 GitHub main 对齐"
exit 0
