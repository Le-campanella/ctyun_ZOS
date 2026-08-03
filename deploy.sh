#!/usr/bin/env bash
set -Eeuo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
config_file="${DEPLOY_CONFIG:-$repo_dir/.deploy.env}"
if [[ -f "$config_file" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$config_file"
  set +a
fi

required=(DEPLOY_TARGET DEPLOY_REMOTE_DIR DEPLOY_HEALTH_URL DEPLOY_READY_URL DEPLOY_SSH_KEY)
for name in "${required[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    echo "部署已取消：缺少 $name；请复制 .deploy.env.example 为 .deploy.env。" >&2
    exit 2
  fi
done
[[ "$DEPLOY_TARGET" =~ ^[A-Za-z0-9._-]+@[A-Za-z0-9.:-]+$ ]] || {
  echo "DEPLOY_TARGET 格式无效。" >&2
  exit 2
}
[[ "$DEPLOY_REMOTE_DIR" =~ ^[A-Za-z0-9._/-]+$ && "$DEPLOY_REMOTE_DIR" != /* && "$DEPLOY_REMOTE_DIR" != *..* ]] || {
  echo "DEPLOY_REMOTE_DIR 必须是远程用户主目录下的相对路径。" >&2
  exit 2
}
[[ -r "$DEPLOY_SSH_KEY" ]] || {
  echo "DEPLOY_SSH_KEY 不可读。" >&2
  exit 2
}

ssh_options=(-i "$DEPLOY_SSH_KEY" -o BatchMode=yes -o ConnectTimeout=10)
remote_path="\$HOME/$DEPLOY_REMOTE_DIR"
project=ctyun_zos
tag="$(git -C "$repo_dir" rev-parse --short=12 HEAD)"
image="zos-upload-service:$tag"
new_schema=
backup_name="deploy-$(date -u +%Y%m%dT%H%M%SZ)-schema"
deployed=0
previous_image=
previous_schema=
database_volume=

rollback() {
  status=$?
  trap - ERR
  if [[ "$deployed" != 1 || -z "$previous_image" ]]; then
    exit "$status"
  fi
  echo "部署检查失败，开始回滚到 $previous_image。" >&2
  previous_tag="${previous_image##*:}"
  ssh "${ssh_options[@]}" "$DEPLOY_TARGET" \
    "cd \"$remote_path\" && docker compose --project-name \"$project\" stop zos-upload"
  if [[ "$previous_schema" != "$new_schema" ]]; then
    ssh "${ssh_options[@]}" "$DEPLOY_TARGET" \
      "docker run --rm -v \"$database_volume:/data/db\" \"$image\" python -c 'import os,pathlib,sqlite3,sys; src=sys.argv[1]; dst=sys.argv[2]; [pathlib.Path(dst+s).unlink(missing_ok=True) for s in (\"-wal\",\"-shm\")]; source=sqlite3.connect(src); target=sqlite3.connect(dst); source.backup(target); target.close(); source.close()' \"/data/db/deploy-backups/$backup_name-$previous_schema.sqlite3\" /data/db/zos-upload.db"
  fi
  ssh "${ssh_options[@]}" "$DEPLOY_TARGET" \
    "cd \"$remote_path\" && IMAGE_TAG=\"$previous_tag\" docker compose --project-name \"$project\" up -d --no-build --force-recreate"
  echo "已回滚；迁移前数据库备份仍保留在 zos-database:/data/db/deploy-backups/。" >&2
  exit "$status"
}
trap rollback ERR

cd "$repo_dir"
if [[ -n "$(git status --porcelain)" ]]; then
  echo "部署已取消：请先提交或清理本地改动。" >&2
  exit 1
fi

docker build --target test -t "${image}-test" .
docker run --rm "${image}-test"
docker build --target runtime -t "$image" .
new_schema="$(docker run --rm "$image" python -c 'from app.database import SCHEMA_VERSION; print(SCHEMA_VERSION)')"

ssh "${ssh_options[@]}" "$DEPLOY_TARGET" \
  "docker info >/dev/null && docker compose version >/dev/null && mkdir -p \"$remote_path\""

container="$(ssh "${ssh_options[@]}" "$DEPLOY_TARGET" \
  "cd \"$remote_path\" 2>/dev/null && docker compose --project-name \"$project\" ps -q zos-upload" || true)"
if [[ -n "$container" ]]; then
  previous_image="$(ssh "${ssh_options[@]}" "$DEPLOY_TARGET" \
    "docker inspect --format='{{.Config.Image}}' \"$container\"")"
  database_volume="$(ssh "${ssh_options[@]}" "$DEPLOY_TARGET" \
    "docker inspect --format='{{range .Mounts}}{{if eq .Destination \"/data/db\"}}{{.Name}}{{end}}{{end}}' \"$container\"")"
  previous_schema="$(ssh "${ssh_options[@]}" "$DEPLOY_TARGET" \
    "docker exec \"$container\" python -c 'import os,sqlite3; print(sqlite3.connect(os.getenv(\"DATABASE_PATH\",\"/data/db/zos-upload.db\")).execute(\"PRAGMA user_version\").fetchone()[0])'")"
  ssh "${ssh_options[@]}" "$DEPLOY_TARGET" \
    "docker exec \"$container\" python -c 'import os,pathlib,sqlite3,sys; src=os.getenv(\"DATABASE_PATH\",\"/data/db/zos-upload.db\"); dst=sys.argv[1]; pathlib.Path(dst).parent.mkdir(parents=True,exist_ok=True); source=sqlite3.connect(src); target=sqlite3.connect(dst); source.backup(target); target.close(); source.close()' \"/data/db/deploy-backups/$backup_name-$previous_schema.sqlite3\""
  echo "远程数据库已备份：schema $previous_schema。"
fi

git archive --format=tar HEAD | gzip | ssh "${ssh_options[@]}" "$DEPLOY_TARGET" \
  "gzip -d | tar -xf - -C \"$remote_path\""

if ! ssh "${ssh_options[@]}" "$DEPLOY_TARGET" "test -f \"$remote_path/.env\""; then
  ssh "${ssh_options[@]}" "$DEPLOY_TARGET" \
    "cp \"$remote_path/.env.example\" \"$remote_path/.env\""
  echo "首次部署配置已生成：$DEPLOY_TARGET:$DEPLOY_REMOTE_DIR/.env" >&2
  exit 2
fi
if ! ssh "${ssh_options[@]}" "$DEPLOY_TARGET" \
  "grep -Eq '^SETTINGS_ENCRYPTION_KEY=.+$' \"$remote_path/.env\""; then
  echo "部署已取消：服务器 .env 的 SETTINGS_ENCRYPTION_KEY 为空。" >&2
  exit 2
fi

docker save "$image" | gzip | ssh "${ssh_options[@]}" "$DEPLOY_TARGET" "gzip -d | docker load"
deployed=1
ssh "${ssh_options[@]}" "$DEPLOY_TARGET" \
  "cd \"$remote_path\" && IMAGE_TAG=\"$tag\" docker compose --project-name \"$project\" up -d --no-build --force-recreate"

curl --fail --silent --show-error --retry 20 --retry-all-errors --retry-delay 2 "$DEPLOY_HEALTH_URL"
echo
curl --fail --silent --show-error --retry 20 --retry-all-errors --retry-delay 2 "$DEPLOY_READY_URL"
echo
trap - ERR
echo "部署成功：$image -> $DEPLOY_TARGET:$DEPLOY_REMOTE_DIR"
