#!/usr/bin/env bash
set -Eeuo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -f "$repo_dir/.backup.env" ]] || {
  echo "请先配置 .backup.env 并成功执行一次手动备份。" >&2
  exit 2
}
line="17 2 * * * cd \"$repo_dir\" && /bin/bash ./scripts/zos-backup.sh create >> ./backup.log 2>&1 # ctyun-zos-backup"
{
  crontab -l 2>/dev/null | grep -v '# ctyun-zos-backup$' || true
  echo "$line"
} | crontab -
echo "已安装每日 02:17 的私有 ZOS 备份任务。"
