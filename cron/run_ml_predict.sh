#!/bin/bash
# ML 涨停封单连板预测：容器内跑 ml_seal_strength.py，产物写 workspace/ml_seal_strength.json。
# 由 run_daily_scan.sh 末尾的 push_candidates.py 汇总进飞书候选表。
#
# 旧版在主机直接 `python /srv/anengos/...`：主机既无 python 也无容器内目录，
# cron 静默失败（无日志重定向、未走 docker exec），导致 json 停更。本版统一容器包装。
#
# crontab 工作日 15:30（收盘后东财涨停池当日已出）：
#   30 15 * * 1-5 /home/ubuntu/run_ml_predict.sh >> /home/ubuntu/ml_predict.log 2>&1
#
# 防重入：flock 单实例锁；timeout 兜底。
set -u

LOCK=/home/ubuntu/.anengos_ml.lock
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date '+%F %T') 上一轮 ML 预测仍在运行，本轮跳过"
  exit 0
fi

echo "$(date '+%F %T') 开始 ML 封单预测"
timeout 900 sudo docker exec -e PYTHONPATH=/srv/anengos -w /srv/anengos anengos \
  python connectors/ml_seal_strength.py
rc=$?
echo "$(date '+%F %T') 完成 ML 封单预测(exit=$rc)"
exit $rc
