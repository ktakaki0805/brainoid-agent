"""Coarse local observations only: neither link nor clock imply cloud readiness."""

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def local_diagnostics(state_dir: Path) -> dict[str, object]:
    network = "unknown"
    try:
        links = [
            (item / "operstate").read_text().strip()
            for item in Path("/sys/class/net").iterdir()
            if item.name != "lo"
        ]
        network = "link_up" if "up" in links else "link_down"
    except OSError:
        pass
    synchronized = "unconfirmed"
    try:
        result = subprocess.run(
            ["/usr/bin/timedatectl", "show", "--property=NTPSynchronized", "--value"],
            capture_output=True,
            timeout=1,
            check=False,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        )
        if result.returncode == 0 and result.stdout.strip() in {b"yes", b"no"}:
            synchronized = "synchronized" if result.stdout.strip() == b"yes" else "unsynchronized"
    except (OSError, subprocess.TimeoutExpired):
        pass
    free_bytes: int | None = None
    try:
        free_bytes = shutil.disk_usage(state_dir).free
    except OSError:
        pass
    return {
        "network": network,
        "clock": synchronized,
        "observed_at": datetime.now(UTC).isoformat(),
        "free_bytes": free_bytes,
        "cloud": "unavailable",
        "audio": "unavailable",
        "remote_revocation": "unconfirmed",
    }
