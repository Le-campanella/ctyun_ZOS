from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_deploy_script_is_valid_and_has_no_environment_specific_defaults():
    subprocess.run(["bash", "-n", "deploy.sh"], cwd=ROOT, check=True)
    subprocess.run(["bash", "-n", "scripts/accept-zos.sh"], cwd=ROOT, check=True)
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
