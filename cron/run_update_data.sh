#!/bin/bash
# 交易日收盘后增量刷新全A日线（Tushare 源；baostock 被列入黑名单后的备用主路径）
# 由 crontab 工作日调用；stdout/stderr 重定向由 cron 负责。
echo $(date '+%Y-%m-%d %H:%M:%S') '开始行情增量更新(tushare)'
sudo docker exec -w /srv/anengos anengos python /srv/anengos/update_incremental.py
echo $(date '+%Y-%m-%d %H:%M:%S') '完成行情增量更新'
