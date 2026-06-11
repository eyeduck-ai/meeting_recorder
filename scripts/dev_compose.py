"""Run Docker Compose with a workspace-isolated dev/test identity."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from scripts.docker_runtime_guard import (
    DEFAULT_DEV_APP_PORT,
    DEFAULT_DEV_VNC_PORT,
    DockerGuardError,
    assert_dev_runtime_safe,
    build_dev_identity,
    collect_docker_containers,
)


def main(argv: list[str] | None = None) -> int:
    """Run `docker compose` after applying dev/test isolation defaults."""

    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        args = ["ps"]

    cwd = Path.cwd().resolve()
    env = os.environ.copy()
    identity = build_dev_identity(cwd, env)
    env.setdefault("COMPOSE_PROJECT_NAME", identity.project)
    env.setdefault("APP_PORT", str(DEFAULT_DEV_APP_PORT))
    env.setdefault("VNC_PORT", str(DEFAULT_DEV_VNC_PORT))
    env.setdefault("MEETING_RECORDER_IMAGE", identity.image)

    try:
        containers = collect_docker_containers()
        assert_dev_runtime_safe(identity, containers)
    except DockerGuardError as exc:
        print("Refusing unsafe dev/test Docker compose invocation:", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 2
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(f"Could not inspect Docker runtime before compose: {exc}", file=sys.stderr)
        return 2

    print(
        "Using dev compose identity: "
        f"project={env['COMPOSE_PROJECT_NAME']} "
        f"app_port={env['APP_PORT']} "
        f"vnc_port={env['VNC_PORT']} "
        f"image={env['MEETING_RECORDER_IMAGE']}",
        file=sys.stderr,
    )
    return subprocess.call(["docker", "compose", "-p", env["COMPOSE_PROJECT_NAME"], *args], env=env)


if __name__ == "__main__":
    raise SystemExit(main())
