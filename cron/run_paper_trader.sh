#!/bin/bash
# 虚拟盘日更：容器内跑 paper_trader（卖出规则 + 候选买入 + 次日挂单 + 飞书复盘同步）。
# crontab 工作日 15:35：
#   35 15 * * 1-5 /home/ubuntu/run_paper_trader.sh >> /home/ubuntu/paper_trader.log 2>&1
# 防重入：flock 单实例锁；timeout 兜底防僵尸长进程。
set -u

LOCK=/home/ubuntu/.anengos_paper.lock
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date '+%F %T') 上一轮虚拟盘日更仍在运行，本轮跳过"
  exit 0
fi

CODE="from pathlib import Path; from connectors.paper_trader import PaperTraderAdapter; \
print(PaperTraderAdapter().submit('', str(Path('/srv/anengos/workspace'))).text)"

echo "$(date '+%F %T') 开始虚拟盘日更"
timeout 1200 sudo docker exec -e PYTHONPATH=/srv/anengos -w /srv/anengos anengos python -c "$CODE"
rc=$?
echo "$(date '+%F %T') 虚拟盘日更结束(exit=$rc)"
exit $rc
