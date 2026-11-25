#!/bin/bash
# 日更二四棒扫描（行情落库后）：全A离线识别峰级突破 / 接近突破信号，
# 产物写 workspace/esb_scan_output/*.json/.md；随后同步飞书"交易系统（二四棒）"表。
#
# crontab 工作日 16:00（update_data 15:35 落库、paper_trader 15:50 重估之后；产物供次日虚拟盘读取）：
#   0 16 * * 1-5 /home/ubuntu/run_daily_scan.sh >> /home/ubuntu/daily_scan.log 2>&1
#
# 防重入：flock 单实例锁；timeout 兜底防僵尸长进程。扫描失败不阻断写表步骤。
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

run_adapter quant_esb_scan QuantEsbScanAdapter "全A二四棒离线扫描"

echo "$(date '+%F %T') 同步二四棒信号到飞书"
timeout 300 sudo docker exec -e PYTHONPATH=/srv/anengos -w /srv/anengos anengos \
  python connectors/push_esb_signals.py
echo "$(date '+%F %T') 二四棒写表结束(exit=$?)"
exit 0
