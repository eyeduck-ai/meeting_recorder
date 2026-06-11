"""Guardrails for keeping dev/test Docker runtimes isolated."""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

RESERVED_PROJECT_NAMES = {
    "meeting_recorder",
    "meeting-recorder",
    "meeting-recorder-prod",
}
RESERVED_CONTAINER_NAMES = {"meeting-recorder"}
DEFAULT_DEV_APP_PORT = 8001
DEFAULT_DEV_VNC_PORT = 5901


class DockerGuardError(RuntimeError):
    """Raised when a Docker runtime identity would collide with another runtime."""


@dataclass(frozen=True)
class RuntimeIdentity:
    """The compose identity a command intends to use."""

    project: str
    working_dir: str
    image: str
    host_ports: frozenset[int]
    container_name: str | None = None


@dataclass(frozen=True)
class ContainerInfo:
    """Relevant Docker metadata for conflict checks."""

    container_id: str
    name: str
    image: str
    project: str | None
    working_dir: str | None
    service: str | None
    host_ports: frozenset[int]


def workspace_hash(path: str | Path) -> str:
    """Return a stable short hash for a workspace path."""

    resolved = str(Path(path).resolve()).lower()
    return hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:8]


def build_dev_identity(cwd: str | Path, env: dict[str, str]) -> RuntimeIdentity:
    """Build the isolated dev/test Docker identity for a workspace."""

    root = Path(cwd).resolve()
    suffix = workspace_hash(root)
    app_port = int(env.get("APP_PORT", str(DEFAULT_DEV_APP_PORT)))
    vnc_port = int(env.get("VNC_PORT", str(DEFAULT_DEV_VNC_PORT)))
    return RuntimeIdentity(
        project=env.get("COMPOSE_PROJECT_NAME", f"meeting-recorder-dev-{suffix}"),
        working_dir=str(root),
        image=env.get("MEETING_RECORDER_IMAGE", f"meeting-recorder:dev-{suffix}"),
        host_ports=frozenset({app_port, vnc_port}),
        container_name=env.get("MEETING_RECORDER_CONTAINER_NAME") or None,
    )


def _normalized_path(path: str | None) -> str | None:
    if not path:
        return None
    return str(Path(path).resolve()).lower()


def validate_dev_runtime(identity: RuntimeIdentity, containers: list[ContainerInfo]) -> list[str]:
    """Return guard errors for an unsafe dev/test Docker identity."""

    errors: list[str] = []
    intended_dir = _normalized_path(identity.working_dir)

    if identity.project in RESERVED_PROJECT_NAMES:
        errors.append(
            f"Refusing to use reserved compose project '{identity.project}' for dev/test. "
            "Use python -m scripts.dev_compose or set a unique COMPOSE_PROJECT_NAME."
        )

    if identity.container_name in RESERVED_CONTAINER_NAMES:
        errors.append(
            f"Refusing to use reserved container name '{identity.container_name}' for dev/test. "
            "Do not set MEETING_RECORDER_CONTAINER_NAME for dev/test runs."
        )

    for container in containers:
        container_dir = _normalized_path(container.working_dir)
        same_workspace = bool(intended_dir and container_dir and intended_dir == container_dir)
        source = container.working_dir or "unknown working_dir"
        label = f"{container.name} ({container.container_id[:12]})"

        if container.project == identity.project and not same_workspace:
            errors.append(
                f"Compose project '{identity.project}' already exists for {label} from {source}; "
                "choose an isolated project name."
            )

        if identity.container_name and container.name == identity.container_name and not same_workspace:
            errors.append(
                f"Container name '{identity.container_name}' is already used by {label} from {source}; "
                "do not reuse production container names for dev/test."
            )

        shared_ports = sorted(identity.host_ports.intersection(container.host_ports))
        if shared_ports and not same_workspace:
            ports = ", ".join(str(port) for port in shared_ports)
            errors.append(
                f"Host port(s) {ports} are already used by {label} from {source}; "
                "set APP_PORT/VNC_PORT to isolated values."
            )

        if identity.image and container.image == identity.image and not same_workspace:
            errors.append(
                f"Image tag '{identity.image}' is already used by {label} from {source}; "
                "set MEETING_RECORDER_IMAGE to a workspace-specific tag."
            )

    return errors


def collect_docker_containers() -> list[ContainerInfo]:
    """Collect Docker container metadata used by the guard."""

    ids = subprocess.check_output(
        ["docker", "ps", "-a", "--format", "{{.ID}}"],
        text=True,
    ).splitlines()
    if not ids:
        return []

    raw = subprocess.check_output(["docker", "inspect", *ids], text=True)
    inspected = json.loads(raw)
    containers: list[ContainerInfo] = []
    for item in inspected:
        labels = item.get("Config", {}).get("Labels") or {}
        ports = set()
        network_ports = item.get("NetworkSettings", {}).get("Ports") or {}
        for bindings in network_ports.values():
            if not bindings:
                continue
            for binding in bindings:
                try:
                    ports.add(int(binding.get("HostPort")))
                except (TypeError, ValueError):
                    continue

        name = str(item.get("Name") or "").lstrip("/")
        containers.append(
            ContainerInfo(
                container_id=str(item.get("Id") or ""),
                name=name,
                image=str(item.get("Config", {}).get("Image") or ""),
                project=labels.get("com.docker.compose.project"),
                working_dir=labels.get("com.docker.compose.project.working_dir"),
                service=labels.get("com.docker.compose.service"),
                host_ports=frozenset(ports),
            )
        )
    return containers


def assert_host_ports_available(identity: RuntimeIdentity, containers: list[ContainerInfo]) -> None:
    """Reject host ports already occupied outside the intended workspace."""

    container_ports = set()
    intended_dir = _normalized_path(identity.working_dir)
    for container in containers:
        if _normalized_path(container.working_dir) == intended_dir:
            container_ports.update(container.host_ports)

    for port in sorted(identity.host_ports):
        if port in container_ports:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError as exc:
                raise DockerGuardError(
                    f"Host port {port} is already in use. Set APP_PORT/VNC_PORT to isolated values."
                ) from exc


def assert_dev_runtime_safe(identity: RuntimeIdentity, containers: list[ContainerInfo]) -> None:
    """Raise when the intended dev/test Docker identity is unsafe."""

    errors = validate_dev_runtime(identity, containers)
    if errors:
        raise DockerGuardError("\n".join(f"- {error}" for error in errors))
    assert_host_ports_available(identity, containers)
