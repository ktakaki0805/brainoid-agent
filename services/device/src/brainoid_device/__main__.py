"""Explicit check/serve commands; SIGINT/SIGTERM own the service lifetime."""

import argparse
import logging
import sys

from brainoid_device.app import DeviceApplication
from brainoid_device.config import ConfigurationError, load_settings
from brainoid_device.server import serve


def main() -> int:
    parser = argparse.ArgumentParser(prog="brainoid-device")
    parser.add_argument("command", choices=("check", "serve"))
    args = parser.parse_args()
    # Runtime libraries must never print URLs, headers, credential paths or tracebacks.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "asyncio"):
        logger = logging.getLogger(name)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
    application: DeviceApplication | None = None
    try:
        settings = load_settings()
        application = DeviceApplication(settings)
        if args.command == "check":
            if application.storage.snapshot().status not in {"unregistered", "ready"}:
                print("device_storage_recovery_required", file=sys.stderr)
                return 1
            print("device_configuration_ok")
            return 0
        print("device_starting", flush=True)
        if not serve(application, settings):
            print("device_lifecycle_failed", file=sys.stderr)
            return 1
        print("device_stopped", flush=True)
        return 0
    except ConfigurationError as exc:
        print(f"device_configuration_error: {exc}", file=sys.stderr)
        return 2
    except (Exception, SystemExit):
        print("device_runtime_failed", file=sys.stderr)
        return 1
    finally:
        if application is not None:
            application.close()


if __name__ == "__main__":
    raise SystemExit(main())
