#!/bin/bash
# guard_download.sh — 行情库守护
# 2026-11-12 起切换：baostock 服务器 IP 被列入黑名单(error 10001011)，
# 旧 download_unified.py 登录即失败、空转刷日志，故不再自动拉起。
# 每日行情改由 Tushare 增量脚本 update_incremental.py 经 cron(工作日 16:40) 负责，
# 按交易日全市场回补，并自动纳入新上市沪深股票；本脚本只观测只数、记录日志。
# 若日后 baostock 解封，把 ENABLE_BS_DOWNLOAD 置回 1 即可恢复旧全量下载守护。
set -u
GUARD_LOG=/home/ubuntu/guard_download.log
DONE_THRESHOLD=5500
ENABLE_BS_DOWNLOAD=0

CNT=$(sudo docker exec anengos python -c "import sqlite3;c=sqlite3.connect('/srv/anengos/data/a_stock_daily.db');print(c.execute('select count(distinct code) from daily').fetchone()[0])" 2>/dev/null | tr -dc '0-9')
CNT=${CNT:-0}

if [ "$CNT" -ge "$DONE_THRESHOLD" ]; then
  echo "$(date '+%F %T') done cnt=$CNT >= $DONE_THRESHOLD" >> "$GUARD_LOG"
  exit 0
fi

if [ "$ENABLE_BS_DOWNLOAD" -eq 1 ]; then
  RUN=$(sudo docker top anengos 2>/dev/null | grep -c 'download_unified.py')
  if [ "$RUN" -eq 0 ]; then
    echo "$(date '+%F %T') downloader missing (cnt=$CNT), restarting" >> "$GUARD_LOG"
    sudo docker exec -d anengos bash -c 'cd /srv/anengos && python -u download_unified.py >> /tmp/download_unified.log 2>&1'
  else
    echo "$(date '+%F %T') alive cnt=$CNT" >> "$GUARD_LOG"
  fi
else
  echo "$(date '+%F %T') observe cnt=$CNT (baostock blocked; daily refresh via tushare incremental cron)" >> "$GUARD_LOG"
fi
