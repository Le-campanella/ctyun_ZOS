#!/usr/bin/env bash
set -Eeuo pipefail

base_url="${ACCEPT_BASE_URL:?请设置 ACCEPT_BASE_URL，例如 http://server:8000}"
preset="${ACCEPT_PRESET:-}"
size_mib="${ACCEPT_SIZE_MIB:-20}"
concurrency="${ACCEPT_CONCURRENCY:-4}"
[[ "$size_mib" =~ ^[1-9][0-9]*$ && "$concurrency" =~ ^[1-9][0-9]*$ ]] || {
  echo "ACCEPT_SIZE_MIB 和 ACCEPT_CONCURRENCY 必须是正整数。" >&2
  exit 2
}

work_dir="$(mktemp -d)"
trap 'rm -rf -- "$work_dir"' EXIT
sample="$work_dir/acceptance-${size_mib}MiB.bin"
truncate -s "$((size_mib * 1024 * 1024))" "$sample"
run_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"

upload_and_delete() {
  id="$1"
  response="$work_dir/upload-$id.json"
  delete_response="$work_dir/delete-$id.json"
  headers=(-H "Accept: application/json" -H "X-Request-ID: acceptance-$run_id-$id" -H "Idempotency-Key: acceptance-$run_id-$id")
  if [[ -n "$preset" ]]; then
    headers+=(-H "X-Storage-Preset: $preset")
  fi

  curl --silent --show-error --fail-with-body --max-time 900 \
    "${headers[@]}" -F "file=@$sample;type=application/octet-stream" \
    "$base_url/v1/uploads" >"$response"
  mapfile -t result < <(python3 -c '
import json, sys
body = json.load(open(sys.argv[1]))
for key in ("task_id", "url", "delete_token", "etag"):
    assert body.get(key), f"missing {key}"
assert body["size_bytes"] == int(sys.argv[2])
print(body["task_id"])
print(body["url"])
print(body["delete_token"])
' "$response" "$((size_mib * 1024 * 1024))")

  curl --silent --show-error --fail --head --max-time 30 "${result[1]}" >/dev/null
  curl --silent --show-error --fail-with-body --max-time 120 -X DELETE \
    -H "Accept: application/json" -H "X-Delete-Token: ${result[2]}" \
    "$base_url/v1/upload-tasks/${result[0]}/object" >"$delete_response"
  python3 -c '
import json, sys
body = json.load(open(sys.argv[1]))
assert body["object_status"] == "deleted"
' "$delete_response"
  echo "任务 ${result[0]}：${size_mib} MiB 上传、公网 HEAD、严格删除通过"
}

pids=()
for id in $(seq 1 "$concurrency"); do
  upload_and_delete "$id" &
  pids+=("$!")
done
failed=0
for pid in "${pids[@]}"; do
  wait "$pid" || failed=1
done
[[ "$failed" == 0 ]] || exit 1
echo "真实 ZOS 验收通过：并发 $concurrency，单文件 ${size_mib} MiB，预设 ${preset:-默认}"
