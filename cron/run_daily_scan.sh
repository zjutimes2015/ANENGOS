#!/bin/bash
# 日更候选扫描（行情落库后）：全A离线低波池 + 多因子低波 + 沪深300/中证500 技术筛选，
# 各产物写 workspace/*_output/*.md，最后汇总三源候选统一写飞书名单表。
#
# crontab 工作日 16:50（update_data 16:40 增量落库之后；产物供次日虚拟盘读取）：
#   50 16 * * 1-5 /home/ubuntu/run_daily_scan.sh >> /home/ubuntu/daily_scan.log 2>&1
#
# 防重入：flock 单实例锁；timeout 兜底防僵尸长进程。各信号独立失败不阻断后续。
set -u

LOCK=/home/ubuntu/.anengos_scan.lock
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date '+%F %T') 上一轮候选扫描仍在运行，本轮跳过"
  exit 0
fi

WS="Path('/srv/anengos/workspace')"

run_adapter() {
  local module="$1"; local cls="$2"; local label="$3"
  echo "$(date '+%F %T') 开始 $label"
  timeout 900 sudo docker exec -e PYTHONPATH=/srv/anengos -w /srv/anengos anengos \
    python -c "from pathlib import Path; from connectors.$module import $cls; \
print($cls().submit('', str($WS)).text)"
  echo "$(date '+%F %T') 完成 $label(exit=$?)"
}

run_adapter quant_all_a QuantAllAAdapter "全A低波离线扫描"
run_adapter quant_multifactor QuantMultifactorAdapter "多因子低波扫描"
run_adapter quant_screener QuantScreenerAdapter "沪深300/中证500技术筛选"

echo "$(date '+%F %T') 汇总候选写飞书名单表"
timeout 300 sudo docker exec -e PYTHONPATH=/srv/anengos -w /srv/anengos anengos \
  python connectors/push_candidates.py
echo "$(date '+%F %T') 候选写表结束(exit=$?)"
exit 0
