#!/usr/bin/env bash
set -Eeuo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
command="${1:-create}"
[[ "$command" == create || "$command" == create-verify || "$command" == verify || "$command" == restore ]] || {
  echo "用法：$0 create | create-verify | verify <object-key> | restore <object-key> <output-dir>" >&2
  exit 2
}
if [[ "$command" == verify && $# != 2 ]]; then
  echo "verify 必须提供备份对象 Key。" >&2
  exit 2
fi
if [[ "$command" == restore && $# != 3 ]]; then
  echo "restore 必须提供备份对象 Key 和不存在的输出目录。" >&2
  exit 2
fi

backup_env="${BACKUP_ENV:-$repo_dir/.backup.env}"
service_env="$repo_dir/.env"
[[ -f "$backup_env" ]] || {
  echo "缺少 .backup.env。" >&2
  exit 2
}
if [[ ("$command" == create || "$command" == create-verify) && ! -f "$service_env" ]]; then
  echo "$command 需要 .env 中的 SETTINGS_ENCRYPTION_KEY。" >&2
  exit 2
fi
mode="$(stat -c %a "$backup_env")"
if (( (8#$mode & 077) != 0 )); then
  echo ".backup.env 权限必须是 600。" >&2
  exit 2
fi

exec 9>"$repo_dir/.backup.lock"
flock -n 9 || {
  echo "已有备份任务正在运行。" >&2
  exit 1
}

container="$(docker compose --project-directory "$repo_dir" --project-name ctyun_zos ps -q zos-upload 2>/dev/null || true)"
if [[ "$command" == create || "$command" == create-verify ]]; then
  [[ -n "$container" ]] || {
    echo "上传服务容器未运行。" >&2
    exit 1
  }
  image="$(docker inspect --format='{{.Config.Image}}' "$container")"
  database_volume="$(docker inspect --format='{{range .Mounts}}{{if eq .Destination "/data/db"}}{{.Name}}{{end}}{{end}}' "$container")"
else
  image="${BACKUP_IMAGE:-}"
  if [[ -z "$image" && -n "$container" ]]; then
    image="$(docker inspect --format='{{.Config.Image}}' "$container")"
  fi
  if [[ -z "$image" ]]; then
    image="zos-upload-backup-tool:local"
    docker build --target runtime -t "$image" "$repo_dir"
  fi
fi
args=("$command")
[[ "$command" == verify || "$command" == restore ]] && args+=("$2")
docker_args=(--rm --init --env-file "$backup_env")
if [[ "$command" == create || "$command" == create-verify ]]; then
  args=("$command")
  docker_args+=(--env-file "$service_env" -v "$database_volume:/data/db")
elif [[ "$command" == restore ]]; then
  output_directory="$(realpath -m "$3")"
  [[ ! -e "$output_directory" && -d "$(dirname "$output_directory")" ]] || {
    echo "输出目录必须不存在，且父目录必须存在。" >&2
    exit 2
  }
  output_name="$(basename "$output_directory")"
  docker_args+=(
    --user "$(id -u):$(id -g)"
    -e TMPDIR=/tmp
    -v "$(dirname "$output_directory"):/restore"
  )
  args+=("/restore/$output_name")
fi

docker run "${docker_args[@]}" "$image" python -m app.backup "${args[@]}"
