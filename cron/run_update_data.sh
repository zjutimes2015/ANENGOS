#!/bin/bash
# 交易日收盘后增量刷新全A日线（Tushare 源；baostock 被列入黑名单后的备用主路径）
# 由 crontab 工作日调用；stdout/stderr 重定向由 cron 负责。
# 防重入：flock 单实例锁；timeout 兜底防僵尸长进程。
set -u

LOCK=/home/ubuntu/.anengos_update.lock
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date '+%F %T') 上一轮增量更新仍在运行，本轮跳过"
  exit 0
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') 开始行情增量更新(tushare)"
timeout 1800 sudo docker exec -w /srv/anengos anengos python /srv/anengos/update_incremental.py
rc=$?
echo "$(date '+%Y-%m-%d %H:%M:%S') 完成行情增量更新(exit=$rc)"
exit $rc
