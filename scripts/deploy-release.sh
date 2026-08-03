#!/usr/bin/env bash
set -Eeuo pipefail

remote_path="$HOME/$1"
project="$2"
tag="$3"
new_schema="$4"
health_url="$5"
ready_url="$6"
drain_seconds="$7"
keep_releases="$8"
keep_migrations="$9"
max_backup_bytes="${10}"
stage_id="${11}"
image="zos-upload-service:$tag"
staging="$remote_path/.deploy-staging/$stage_id"
candidate="${project}-candidate-$stage_id"
backup_name="deploy-$(date -u +%Y%m%dT%H%M%SZ)-from"
previous_image=
previous_schema=
database_volume=
snapshot_path=
old_stopped=0
snapshot_created=0
release_started=0

remove_candidate() {
  docker rm -f "$candidate" >/dev/null 2>&1 || true
}

restore_release_snapshot() {
  docker run --rm -v "$database_volume:/data/db" "$image" \
    python -m app.deploy_backups restore "$snapshot_path" /data/db/zos-upload.db
}

rollback() {
  status=$?
  trap - ERR
  set +e
  recovery_failed=0
  remove_candidate
  if [[ -n "$previous_image" ]]; then
    if [[ "$release_started" == 1 ]]; then
      (cd "$remote_path" && docker compose --project-name "$project" stop zos-upload)
    fi
    if [[ "$snapshot_created" == 1 ]]; then
      restore_release_snapshot || recovery_failed=1
    fi
    if [[ "$old_stopped" == 1 ]]; then
      previous_tag="${previous_image##*:}"
      (cd "$remote_path" && IMAGE_TAG="$previous_tag" \
        docker compose --project-name "$project" up -d --no-build --force-recreate) || recovery_failed=1
    fi
    if [[ "$recovery_failed" == 0 ]]; then
      echo "发布失败：旧镜像与发布前数据库已恢复。" >&2
    else
      echo "严重错误：自动回滚未完整执行，请保持停服并按快照手动恢复。" >&2
    fi
  elif [[ "$release_started" == 1 ]]; then
    (cd "$remote_path" && docker compose --project-name "$project" down) || recovery_failed=1
    if [[ "$recovery_failed" == 0 ]]; then
      echo "首次发布失败：不健康服务已清理。" >&2
    else
      echo "严重错误：首次发布的不健康服务自动清理失败。" >&2
    fi
  fi
  [[ "$recovery_failed" == 0 ]] || exit 1
  exit "$status"
}

cleanup() {
  remove_candidate
  rm -rf -- "$staging"
}
trap cleanup EXIT
trap rollback ERR

cd "$remote_path"
gzip -dc "$staging/source.tar.gz" | tar -xf - -C "$remote_path"
[[ -f .env ]] || {
  cp .env.example .env
  echo "首次部署配置已生成：$remote_path/.env，请填写后重试。" >&2
  exit 2
}
grep -Eq '^SETTINGS_ENCRYPTION_KEY=.+$' .env || {
  echo "部署已取消：服务器 .env 的 SETTINGS_ENCRYPTION_KEY 为空。" >&2
  exit 2
}
grep -Eq '^ADMIN_API_KEYS=.+$' .env || {
  echo "部署已取消：服务器 .env 的 ADMIN_API_KEYS 为空。" >&2
  exit 2
}
gzip -dc "$staging/image.tar.gz" | docker load >/dev/null

container="$(docker compose --project-name "$project" ps -q --all zos-upload)"
if [[ -n "$container" ]]; then
  previous_image="$(docker inspect --format='{{.Config.Image}}' "$container")"
  database_volume="$(docker inspect --format='{{range .Mounts}}{{if eq .Destination "/data/db"}}{{.Name}}{{end}}{{end}}' "$container")"
  [[ -n "$database_volume" ]] || {
    echo "部署已取消：无法识别数据库卷。" >&2
    exit 1
  }
  docker compose --project-name "$project" stop -t "$drain_seconds" zos-upload
  old_stopped=1
  previous_schema="$(docker run --rm -v "$database_volume:/data/db" "$image" \
    python -c 'import sqlite3; print(sqlite3.connect("file:/data/db/zos-upload.db?mode=ro", uri=True).execute("PRAGMA user_version").fetchone()[0])')"
  snapshot_path="/data/db/deploy-backups/$backup_name-$previous_schema-to-$new_schema.sqlite3"
  docker run --rm -v "$database_volume:/data/db" "$image" \
    python -m app.deploy_backups snapshot /data/db/zos-upload.db "$snapshot_path"
  snapshot_created=1
  echo "发布前数据库快照已校验：schema $previous_schema -> $new_schema。"
fi

release_started=1
IMAGE_TAG="$tag" docker compose --project-name "$project" run -d --no-deps \
  --name "$candidate" zos-upload >/dev/null
for endpoint in healthz readyz; do
  passed=0
  for _ in $(seq 1 30); do
    if docker exec "$candidate" python -c \
      'import sys,urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/"+sys.argv[1], timeout=3).read()' \
      "$endpoint" >/dev/null 2>&1; then
      passed=1
      break
    fi
    sleep 2
  done
  [[ "$passed" == 1 ]] || {
    echo "候选容器内部 /$endpoint 验收失败。" >&2
    false
  }
done
remove_candidate

IMAGE_TAG="$tag" docker compose --project-name "$project" up -d --no-build --force-recreate
for url in "$health_url" "$ready_url"; do
  passed=0
  for _ in $(seq 1 20); do
    if docker run --rm --network host "$image" python -c \
      'import sys,urllib.request; urllib.request.urlopen(sys.argv[1], timeout=3).read()' \
      "$url" >/dev/null 2>&1; then
      passed=1
      break
    fi
    sleep 2
  done
  [[ "$passed" == 1 ]] || {
    echo "局域网入口验收失败：$url" >&2
    false
  }
done

if [[ "$snapshot_created" == 1 ]]; then
  docker run --rm -v "$database_volume:/data/db" "$image" \
    python -m app.deploy_backups prune /data/db/deploy-backups \
      --keep-releases "$keep_releases" \
      --keep-migrations "$keep_migrations" \
      --max-bytes "$max_backup_bytes"
fi
trap - ERR
echo "部署成功：$image"
