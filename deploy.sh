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

DEPLOY_DRAIN_SECONDS="${DEPLOY_DRAIN_SECONDS:-600}"
DEPLOY_BACKUP_KEEP_RELEASES="${DEPLOY_BACKUP_KEEP_RELEASES:-10}"
DEPLOY_BACKUP_KEEP_MIGRATIONS="${DEPLOY_BACKUP_KEEP_MIGRATIONS:-3}"
DEPLOY_BACKUP_MAX_BYTES="${DEPLOY_BACKUP_MAX_BYTES:-1073741824}"
for name in DEPLOY_DRAIN_SECONDS DEPLOY_BACKUP_KEEP_RELEASES \
  DEPLOY_BACKUP_KEEP_MIGRATIONS DEPLOY_BACKUP_MAX_BYTES; do
  [[ "${!name}" =~ ^[1-9][0-9]*$ ]] || {
    echo "$name 必须是大于 0 的整数。" >&2
    exit 2
  }
done

ssh_options=(-i "$DEPLOY_SSH_KEY" -o BatchMode=yes -o ConnectTimeout=10)
project=ctyun_zos
tag="$(git -C "$repo_dir" rev-parse --short=12 HEAD)"
stage_id="$tag-$$"
image="zos-upload-service:$tag"
remote_path="\$HOME/$DEPLOY_REMOTE_DIR"
remote_staging="$remote_path/.deploy-staging/$stage_id"
staged=0

cleanup() {
  if [[ "$staged" == 1 ]]; then
    ssh "${ssh_options[@]}" "$DEPLOY_TARGET" \
      "rm -rf -- \"$remote_staging\"" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

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
  "docker info >/dev/null && docker compose version >/dev/null && command -v flock >/dev/null && mkdir -p \"$remote_staging\""
staged=1
git archive --format=tar HEAD | gzip | ssh "${ssh_options[@]}" "$DEPLOY_TARGET" \
  "cat > \"$remote_staging/source.tar.gz\""
docker save "$image" | gzip | ssh "${ssh_options[@]}" "$DEPLOY_TARGET" \
  "cat > \"$remote_staging/image.tar.gz\""

remote_command="cd \"$remote_path\" && flock -n -E 75 .deploy.lock bash -s --"
for argument in \
  "$DEPLOY_REMOTE_DIR" "$project" "$tag" "$new_schema" \
  "$DEPLOY_HEALTH_URL" "$DEPLOY_READY_URL" "$DEPLOY_DRAIN_SECONDS" \
  "$DEPLOY_BACKUP_KEEP_RELEASES" "$DEPLOY_BACKUP_KEEP_MIGRATIONS" \
  "$DEPLOY_BACKUP_MAX_BYTES" "$stage_id"; do
  printf -v quoted ' %q' "$argument"
  remote_command+="$quoted"
done

set +e
ssh "${ssh_options[@]}" "$DEPLOY_TARGET" "$remote_command" \
  < "$repo_dir/scripts/deploy-release.sh"
status=$?
set -e
if [[ "$status" == 75 ]]; then
  echo "部署已取消：另一项远程部署正在运行。" >&2
fi
exit "$status"
