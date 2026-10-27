"""Bounded, restartable system integration; never installs packages or erases data."""

import argparse
import hashlib
import json
import os
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path

SOURCE = Path(__file__).resolve().parent
MANIFEST = "etc/brainoid/provision-v1.json"
CONFIG = "etc/brainoid/device.env"
UNITS = ("brainoid-agent.service", "brainoid-kiosk.service")


class ProvisionError(RuntimeError):
    pass


def files() -> dict[str, tuple[bytes, int]]:
    return {
        "etc/systemd/system/brainoid-agent.service": (
            (SOURCE.parent / "systemd/brainoid-agent.service").read_bytes(),
            0o644,
        ),
        "etc/systemd/system/brainoid-kiosk.service": (
            (SOURCE / "brainoid-kiosk.service").read_bytes(),
            0o644,
        ),
        "etc/sysusers.d/brainoid.conf": ((SOURCE / "brainoid.conf").read_bytes(), 0o644),
        "etc/pam.d/brainoid-kiosk": ((SOURCE / "brainoid-kiosk.pam").read_bytes(), 0o644),
        "usr/local/libexec/brainoid-kiosk-browser": (
            (SOURCE / "kiosk-browser").read_bytes(),
            0o755,
        ),
    }


def safe(path: Path, *, directory: bool = False) -> None:
    """Protect privileged writes: all existing ancestors must be stable and trusted."""
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ProvisionError("symlink in installation path")
        if info.st_uid not in {0, os.geteuid()}:
            raise ProvisionError("untrusted installation owner")
        if info.st_mode & 0o022 and not (
            stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        ):
            raise ProvisionError("writable installation path")
        if part != path or directory:
            if not stat.S_ISDIR(info.st_mode):
                raise ProvisionError("installation parent is not a directory")
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ProvisionError("installation file is not a single regular file")


def write_new(path: Path, content: bytes, mode: int) -> None:
    safe(path)
    # Create each missing ancestor privately before setting its intended mode.
    # Do not chmod an existing directory, and never create a writable window
    # under permissive umasks. The selected root/ancestors are already trusted.
    for parent in reversed(path.parents):
        if not parent.exists():
            parent.mkdir(mode=0o700)
            parent.chmod(0o755)
        safe(parent, directory=True)
    # Exclusive creation; an interrupted write is never treated as complete.
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as destination:
        os.fchmod(destination.fileno(), mode)
        destination.write(content)
        destination.flush()
        os.fsync(destination.fileno())
    parent_fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def run(argv: list[str]) -> None:
    subprocess.run(argv, check=True, timeout=30, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})


def provision(
    root: Path,
    *,
    activate: bool = False,
    uninstall: bool = False,
    image: bool = False,
    runner: Callable[[list[str]], None] = run,
) -> None:
    if not root.is_absolute() or root.resolve(strict=True) != root:
        raise ProvisionError("root must be an existing canonical absolute directory")
    safe(root, directory=True)
    if root.stat().st_mode & 0o022:
        raise ProvisionError("selected root must not be group/other writable")
    if root == Path("/") and os.geteuid() != 0:
        raise ProvisionError("host integration requires root")
    if activate and root != Path("/"):
        raise ProvisionError("activation only applies to the host root")
    if image and (activate or root == Path("/")):
        raise ProvisionError("image preparation requires a separate root")
    expected = files()
    manifest_data = (
        json.dumps(
            {name: hashlib.sha256(data).hexdigest() for name, (data, _) in expected.items()},
            sort_keys=True,
            indent=2,
        ).encode()
        + b"\n"
    )
    manifest = root / MANIFEST
    safe(manifest)
    owned = manifest.exists()
    if owned and manifest.read_bytes() != manifest_data:
        raise ProvisionError("different or incomplete provision manifest; inspect before retry")
    if uninstall and not owned:
        raise ProvisionError("no matching installation manifest")
    # Preflight every destination before writing any of them; no replacement or
    # adoption of unrelated units, profiles or configuration.
    for name, (content, mode) in expected.items():
        target = root / name
        safe(target)
        if target.exists() and (
            not owned
            or target.read_bytes() != content
            or stat.S_IMODE(target.stat().st_mode) != mode
        ):
            raise ProvisionError("managed file collision; inspect before retry")
    if image or activate:
        for unit in UNITS:
            link = root / "etc/systemd/system/multi-user.target.wants" / unit
            safe(link.parent, directory=True)
            if link.is_symlink():
                if os.readlink(link) != "/etc/systemd/system/" + unit:
                    raise ProvisionError("boot enable link collision")
            elif link.exists():
                raise ProvisionError("boot enable path collision")
    safe(root / CONFIG)
    defaults = (SOURCE.parent / "systemd/device.env.example").read_text()
    if (root / CONFIG).exists():
        configured = dict(
            line.split("=", 1)
            for line in (root / CONFIG).read_text().splitlines()
            if line and not line.startswith("#") and "=" in line
        )
        required = dict(line.split("=", 1) for line in defaults.splitlines() if line)
        if any(configured.get(key) != value for key, value in required.items()):
            raise ProvisionError("existing device configuration incompatible with fixed kiosk URL")
    # A pre-existing named account is never silently adopted by a first install.
    # NSS/systemd user creation is deliberately only performed during host activation.
    if not owned:
        safe(root / "etc/passwd")
        safe(root / "etc/group")
        for database in ("etc/passwd", "etc/group"):
            if (root / database).exists():
                names = {
                    line.split(":", 1)[0] for line in (root / database).read_text().splitlines()
                }
                if names & {"brainoid-agent", "brainoid-kiosk"}:
                    raise ProvisionError("dedicated account name collision")
    if owned:
        accounts: dict[str, list[str]] = {}
        for database in ("etc/passwd", "etc/group"):
            safe(root / database)
        if (root / "etc/passwd").exists():
            for line in (root / "etc/passwd").read_text().splitlines():
                fields = line.split(":")
                if fields[0] in {"brainoid-agent", "brainoid-kiosk"}:
                    if (
                        len(fields) != 7
                        or int(fields[2]) < 100
                        or int(fields[3]) < 100
                        or fields[5:] != ["/nonexistent", "/usr/sbin/nologin"]
                    ):
                        raise ProvisionError("dedicated account changed")
                    accounts[fields[0]] = fields
            if (
                len(accounts) == 2
                and accounts["brainoid-agent"][2] == accounts["brainoid-kiosk"][2]
            ):
                raise ProvisionError("service and kiosk must have separate UIDs")
    if uninstall:
        if image:
            runner(["systemctl", "--root=" + str(root), "disable", *UNITS])
        if activate:
            runner(["systemctl", "disable", "--now", *UNITS])
        for name in expected:
            (root / name).unlink(missing_ok=True)
        if activate:
            runner(["systemctl", "daemon-reload"])
        # Keep config, accounts, app and all credential/SQLite data. UID reuse and
        # accidental deletion are worse than leaving an explicitly documented remnant.
        return
    if not owned:
        write_new(manifest, manifest_data, 0o600)
    for name, (content, mode) in expected.items():
        if not (root / name).exists():
            write_new(root / name, content, mode)
    if not (root / CONFIG).exists():
        write_new(root / CONFIG, defaults.encode(), 0o600)
    if image:
        runner(["systemd-sysusers", "--root=" + str(root), "brainoid.conf"])
        runner(["systemctl", "--root=" + str(root), "enable", *UNITS])
    if activate:
        for executable in (
            "/opt/brainoid/.venv/bin/python",
            "/usr/bin/cage",
            "/usr/bin/chromium",
            "/usr/bin/curl",
            "/usr/bin/chvt",
        ):
            if not os.access(executable, os.X_OK):
                raise ProvisionError("required runtime executable missing")
        if not Path("/opt/brainoid/display/index.html").is_file():
            raise ProvisionError("built display missing")
        runner(["systemd-sysusers", "/etc/sysusers.d/brainoid.conf"])
        runner(["systemctl", "daemon-reload"])
        runner(["systemctl", "enable", *UNITS])
        # Repeated activation does not restart a running service or kiosk.
        runner(["systemctl", "start", *UNITS])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--prepare-image", action="store_true")
    parser.add_argument("--reserve-tty7", action="store_true")
    args = parser.parse_args()
    try:
        if args.activate and not args.uninstall and not args.reserve_tty7:
            raise ProvisionError("activation requires explicit tty7 reservation")
        provision(
            args.root, activate=args.activate, uninstall=args.uninstall, image=args.prepare_image
        )
    except (OSError, ValueError, ProvisionError, subprocess.SubprocessError):
        print("device_provision_failed: inspect prerequisites/collisions; no data was erased")
        return 1
    print("device_provision_complete" if args.activate else "device_files_staged: not activated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
