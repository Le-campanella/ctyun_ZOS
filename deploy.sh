#!/usr/bin/env bash
set -euo pipefail

target="${DEPLOY_TARGET:-liyang@192.168.1.150}"
remote_dir='services/ctyun_ZOS'
health_url="${DEPLOY_HEALTH_URL:-http://192.168.1.150:8000/healthz}"
ssh_key="${DEPLOY_SSH_KEY:-$HOME/.ssh/id_ed25519_ctyun_zos}"
ssh_options=(-i "$ssh_key" -o BatchMode=yes -o ConnectTimeout=10)

cd "$(git rev-parse --show-toplevel)"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "部署已取消：请先提交或清理本地改动。" >&2
  exit 1
fi

tag="$(git rev-parse --short=12 HEAD)"
image="zos-upload-service:$tag"
remote_path="\$HOME/$remote_dir"

ssh "${ssh_options[@]}" "$target" \
  "docker info >/dev/null && docker compose version >/dev/null && mkdir -p \"$remote_path\""

# ponytail: 远端只运行镜像，覆盖已提交源码即可；需要远端构建时再改成版本化 release 目录。
git archive --format=tar HEAD | gzip | ssh "${ssh_options[@]}" "$target" \
  "gzip -d | tar -xf - -C \"$remote_path\""

if ! ssh "${ssh_options[@]}" "$target" "test -f \"$remote_path/.env\""; then
  ssh "${ssh_options[@]}" "$target" \
    "cp \"$remote_path/.env.example\" \"$remote_path/.env\""
  echo "首次部署配置已生成：$target:~/$remote_dir/.env"
  echo "请填写 SETTINGS_ENCRYPTION_KEY 和服务器 LISTEN_IP 后重新执行 ./deploy.sh。" >&2
  exit 2
fi

if ! ssh "${ssh_options[@]}" "$target" \
  "grep -Eq '^SETTINGS_ENCRYPTION_KEY=.+$' \"$remote_path/.env\""; then
  echo "部署已取消：服务器 .env 的 SETTINGS_ENCRYPTION_KEY 为空。" >&2
  exit 2
fi

docker build --target test -t "${image}-test" .
docker run --rm "${image}-test"
docker build --target runtime -t "$image" .

docker save "$image" | gzip | ssh "${ssh_options[@]}" "$target" "gzip -d | docker load"

ssh "${ssh_options[@]}" "$target" \
  "cd \"$remote_path\" && IMAGE_TAG=\"$tag\" docker compose --project-name ctyun_zos up -d --no-build --force-recreate"

curl --fail --silent --show-error --retry 20 --retry-connrefused --retry-delay 2 "$health_url"
echo
echo "部署成功：$image -> $target:~/$remote_dir"
