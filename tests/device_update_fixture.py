"""Ephemeral Ed25519 TUF repository. Never a production trust anchor/signing tool."""

import io
import json
import platform
import tarfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from brainoid_device.update_files import RUNTIME_ABI
from securesystemslib.signer import CryptoSigner  # type: ignore[attr-defined]
from tuf.api.exceptions import DownloadHTTPError
from tuf.api.metadata import Metadata, MetaFile, Root, Snapshot, TargetFile, Targets, Timestamp  # type: ignore[attr-defined]
from tuf.ngclient.fetcher import FetcherInterface

PROJECT = Path(__file__).resolve().parents[1]


class Repository(FetcherInterface):
    def __init__(self, root: Path) -> None:
        self.path = root
        root.mkdir(mode=0o700)
        (root / "metadata").mkdir()
        (root / "targets").mkdir()
        self.signer = CryptoSigner.generate_ed25519()
        self.expires = datetime.now(UTC) + timedelta(days=2)
        self.root = Metadata(
            Root(
                expires=self.expires,
                consistent_snapshot=False,
                unrecognized_fields={"brainoid_test_only": True},
            )
        )
        for role in ("root", "timestamp", "snapshot", "targets"):
            self.root.signed.add_key(self.signer.public_key, role)
        self.root.sign(self.signer)
        self.trust = self.root.to_bytes()
        self.targets: dict[str, TargetFile] = {}
        self.version = 0
        self.publish()

    def _fetch(self, url: str) -> Iterator[bytes]:
        path = self.path / urlsplit(url).path.lstrip("/")
        if not path.is_file():
            raise DownloadHTTPError("missing synthetic metadata", 404)
        yield path.read_bytes()

    def publish(self, *, expires: datetime | None = None) -> None:
        self.version += 1
        expiry = expires or self.expires
        targets = Metadata(Targets(version=self.version, expires=expiry, targets=self.targets))
        targets.sign(self.signer)
        target_data = targets.to_bytes()
        snapshot = Metadata(
            Snapshot(
                version=self.version,
                expires=expiry,
                meta={"targets.json": MetaFile.from_data(self.version, target_data, ["sha256"])},
            )
        )
        snapshot.sign(self.signer)
        snapshot_data = snapshot.to_bytes()
        timestamp = Metadata(
            Timestamp(
                version=self.version,
                expires=expiry,
                snapshot_meta=MetaFile.from_data(self.version, snapshot_data, ["sha256"]),
            )
        )
        timestamp.sign(self.signer)
        for name, data in (
            ("targets", target_data),
            ("snapshot", snapshot_data),
            ("timestamp", timestamp.to_bytes()),
        ):
            (self.path / "metadata" / f"{name}.json").write_bytes(data)

    def add(
        self,
        version: int,
        *,
        manifest: dict[str, Any] | None = None,
        extra: dict[str, bytes] | None = None,
        broken: bool = False,
    ) -> str:
        value = dict(
            format=1,
            version=version,
            os=platform.system().lower(),
            architecture=platform.machine(),
            protocol=1,
            sqlite_schema=2,
            runtime_abi=RUNTIME_ABI,
        )
        value.update(manifest or {})
        files = {
            "release.json": json.dumps(value).encode(),
            "ui/index.html": f"<p>release {version}</p>".encode(),
        }
        for item in (PROJECT / "services/device/src/brainoid_device").glob("*.py"):
            files["lib/brainoid_device/" + item.name] = item.read_bytes()
        if broken:
            files["lib/brainoid_device/__main__.py"] = b"def main():\n    return 1\n"
        files.update(extra or {})
        target = f"device-{version}.tar"
        data = tar_bytes(files)
        (self.path / "targets" / target).write_bytes(data)
        self.targets[target] = TargetFile.from_data(target, data, ["sha256"])
        self.publish()
        return target

    def rotate(self) -> None:
        old = self.signer
        self.signer = CryptoSigner.generate_ed25519()
        self.root.signed.version += 1
        for role in ("root", "timestamp", "snapshot", "targets"):
            self.root.signed.revoke_key(old.public_key.keyid, role)
            self.root.signed.add_key(self.signer.public_key, role)
        self.root.sign(old)
        self.root.sign(self.signer, append=True)
        (self.path / "metadata" / f"{self.root.signed.version}.root.json").write_bytes(
            self.root.to_bytes()
        )
        self.publish()


def tar_bytes(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, data in files.items():
            item = tarfile.TarInfo(name)
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
    return output.getvalue()


class FakeDaemon:
    def __init__(self) -> None:
        self.active: int | None = None
        self.started: list[int] = []
        self.fail: set[int] = set()
        self.events: list[str] = []

    def start(self, tree: Path) -> None:
        assert self.active is None
        self.active = json.loads((tree / "release.json").read_bytes())["version"]
        assert self.active is not None
        self.started.append(self.active)
        self.events.append(f"start:{self.active}")

    def healthy(self) -> bool:
        return self.active not in self.fail

    def stopped(self) -> bool:
        return self.active is None

    def stop(self) -> None:
        self.events.append("private_audio_stopped")
        self.active = None
