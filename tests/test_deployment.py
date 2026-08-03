from __future__ import annotations

import os
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_deploy_script_is_valid_and_has_no_environment_specific_defaults():
    subprocess.run(["bash", "-n", "deploy.sh"], cwd=ROOT, check=True)
    subprocess.run(["bash", "-n", "scripts/accept-zos.sh"], cwd=ROOT, check=True)
    subprocess.run(["bash", "-n", "scripts/zos-backup.sh"], cwd=ROOT, check=True)
    subprocess.run(["bash", "-n", "scripts/deploy-release.sh"], cwd=ROOT, check=True)
    subprocess.run(
        ["bash", "-n", "scripts/install-backup-cron.sh"], cwd=ROOT, check=True
    )
    script = (ROOT / "deploy.sh").read_text()

    assert "192.168." not in script
    assert "liyang@" not in script
    for name in (
        "DEPLOY_TARGET",
        "DEPLOY_REMOTE_DIR",
        "DEPLOY_HEALTH_URL",
        "DEPLOY_READY_URL",
        "DEPLOY_SSH_KEY",
    ):
        assert name in script
    assert "/readyz" not in script
    assert "deploy-backups" in (ROOT / "scripts/deploy-release.sh").read_text()
    assert "rollback" in (ROOT / "scripts/deploy-release.sh").read_text()

    backup = (ROOT / "scripts/zos-backup.sh").read_text()
    assert ".backup.env" in backup
    assert "public-read" not in backup
    assert "BACKUP_IMAGE" in backup
    assert "zos-upload-backup-tool:local" in backup
    assert "create-verify" in (ROOT / "scripts/install-backup-cron.sh").read_text()


def test_compose_has_bounded_logs_and_restricted_runtime():
    compose = (ROOT / "compose.yaml").read_text()

    for setting in (
        "driver: local",
        'max-size: "10m"',
        'max-file: "3"',
        "cap_drop:",
        "- ALL",
        "read_only: true",
        "pids_limit: 256",
        "/tmp:size=64m,noexec,nosuid,nodev",
    ):
        assert setting in compose


def test_first_deployment_failure_has_explicit_cleanup():
    script = (ROOT / "scripts/deploy-release.sh").read_text()
    assert "docker compose --project-name \"$project\" down" in script


def test_same_schema_rollback_restores_database_snapshot():
    script = (ROOT / "scripts/deploy-release.sh").read_text()
    assert "restore_release_snapshot" in script
    assert '[[ "$snapshot_created" == 1 ]]' in script


def test_release_is_locked_drained_and_checked_before_cutover():
    deploy = (ROOT / "deploy.sh").read_text()
    release = (ROOT / "scripts/deploy-release.sh").read_text()

    assert "flock -n -E 75 .deploy.lock" in deploy
    assert release.index('stop -t "$drain_seconds"') < release.index(
        "app.deploy_backups snapshot"
    )
    assert release.index("compose --project-name \"$project\" run") < release.rindex(
        "compose --project-name \"$project\" up"
    )
    assert "app.deploy_backups prune" in release


@pytest.mark.parametrize(
    ("has_previous", "new_schema"),
    [(False, "4"), (True, "4"), (True, "5")],
)
def test_failed_release_cleans_first_deploy_or_restores_snapshot(
    tmp_path, has_previous, new_schema
):
    home = tmp_path / "home"
    remote = home / "service"
    staging = remote / ".deploy-staging" / "new"
    staging.mkdir(parents=True)
    (remote / ".env").write_text(
        "SETTINGS_ENCRYPTION_KEY=test\nADMIN_API_KEYS=test\n"
    )
    with tarfile.open(staging / "source.tar.gz", "w:gz"):
        pass
    (staging / "image.tar.gz").write_bytes(b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03\x03\x00\x00\x00\x00\x00\x00\x00\x00\x00")

    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
echo "$*" >> "$DOCKER_LOG"
case "$1" in
  load) cat >/dev/null ;;
  compose)
    if [[ "$*" == *"ps -q --all zos-upload"* && "$HAS_PREVIOUS" == 1 ]]; then
      echo old-container
    elif [[ "$*" == *"run -d"* ]]; then
      echo candidate
    fi
    ;;
  inspect)
    if [[ "$*" == *".Config.Image"* ]]; then
      echo zos-upload-service:old
    else
      echo database-volume
    fi
    ;;
  run)
    [[ "$*" == *"PRAGMA user_version"* ]] && echo 4
    ;;
  exec) exit 1 ;;
esac
exit 0
"""
    )
    docker.chmod(0o755)
    sleep = binaries / "sleep"
    sleep.write_text("#!/usr/bin/env bash\nexit 0\n")
    sleep.chmod(0o755)
    log = tmp_path / "docker.log"
    environment = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{binaries}:{os.environ['PATH']}",
        "DOCKER_LOG": str(log),
        "HAS_PREVIOUS": "1" if has_previous else "0",
    }

    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/deploy-release.sh"),
            "service",
            "ctyun_zos",
            "new",
            new_schema,
            "http://server/healthz",
            "http://server/readyz",
            "1",
            "2",
            "1",
            "1024",
            "new",
        ],
        env=environment,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    calls = log.read_text()
    if has_previous:
        assert "app.deploy_backups restore" in calls
        assert "IMAGE_TAG=old" not in calls  # shell assignment is not a Docker argument
        assert "up -d --no-build --force-recreate" in calls
    else:
        assert "compose --project-name ctyun_zos down" in calls
        assert "app.deploy_backups restore" not in calls


def test_verify_can_use_pinned_tool_image_without_running_service(tmp_path):
    backup_env = tmp_path / "backup.env"
    backup_env.write_text("BACKUP_PASSPHRASE=" + "x" * 48 + "\n")
    backup_env.chmod(0o600)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker_log = tmp_path / "docker.log"
    docker = binaries / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
echo "$*" >> "$DOCKER_LOG"
exit 0
"""
    )
    docker.chmod(0o755)

    result = subprocess.run(
        ["bash", str(ROOT / "scripts/zos-backup.sh"), "verify", "prefix/key"],
        env={
            **os.environ,
            "PATH": f"{binaries}:{os.environ['PATH']}",
            "DOCKER_LOG": str(docker_log),
            "BACKUP_ENV": str(backup_env),
            "BACKUP_IMAGE": "backup-tool@sha256:verified",
        },
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    calls = docker_log.read_text()
    assert "backup-tool@sha256:verified python -m app.backup verify prefix/key" in calls
    assert "build --target runtime" not in calls
