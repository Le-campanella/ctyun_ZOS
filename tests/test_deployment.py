from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_deploy_script_is_valid_and_has_no_environment_specific_defaults():
    subprocess.run(["bash", "-n", "deploy.sh"], cwd=ROOT, check=True)
    subprocess.run(["bash", "-n", "scripts/accept-zos.sh"], cwd=ROOT, check=True)
    subprocess.run(["bash", "-n", "scripts/zos-backup.sh"], cwd=ROOT, check=True)
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
    assert "deploy-backups" in script
    assert "rollback" in script

    backup = (ROOT / "scripts/zos-backup.sh").read_text()
    assert ".backup.env" in backup
    assert "public-read" not in backup


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


@pytest.mark.xfail(strict=True, reason="Phase 4 must clean a failed first deployment")
def test_first_deployment_failure_has_explicit_cleanup():
    script = (ROOT / "deploy.sh").read_text()
    assert "docker compose --project-name \"$project\" down" in script


@pytest.mark.xfail(strict=True, reason="Phase 4 must restore same-schema snapshots")
def test_same_schema_rollback_restores_database_snapshot():
    script = (ROOT / "deploy.sh").read_text()
    assert "restore_release_snapshot" in script
