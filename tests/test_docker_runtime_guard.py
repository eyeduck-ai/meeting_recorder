from pathlib import Path

from scripts.docker_runtime_guard import (
    ContainerInfo,
    RuntimeIdentity,
    build_dev_identity,
    validate_dev_runtime,
    workspace_hash,
)


def test_compose_app_service_configures_docker_log_rotation():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    assert "driver: json-file" in compose
    assert 'max-size: "20m"' in compose
    assert 'max-file: "5"' in compose


def _container(
    *,
    name: str = "meeting-recorder",
    project: str = "meeting_recorder",
    working_dir: str = r"E:\code\meeting_recorder",
    image: str = "meeting-recorder:dev",
    ports: set[int] | None = None,
) -> ContainerInfo:
    return ContainerInfo(
        container_id="abcdef1234567890",
        name=name,
        image=image,
        project=project,
        working_dir=working_dir,
        service="app",
        host_ports=frozenset(ports or {8000, 5900}),
    )


def test_workspace_hash_is_stable_for_same_path(tmp_path):
    assert workspace_hash(tmp_path) == workspace_hash(tmp_path)


def test_build_dev_identity_uses_isolated_defaults(tmp_path):
    identity = build_dev_identity(tmp_path, {})

    assert identity.project == f"meeting-recorder-dev-{workspace_hash(tmp_path)}"
    assert identity.image == f"meeting-recorder:dev-{workspace_hash(tmp_path)}"
    assert identity.host_ports == frozenset({8001, 5901})
    assert identity.container_name is None


def test_guard_rejects_same_project_from_different_worktree():
    identity = RuntimeIdentity(
        project="meeting-recorder-dev-test",
        working_dir=r"C:\worktrees\test\meeting_recorder",
        image="meeting-recorder:dev-test",
        host_ports=frozenset({8001, 5901}),
    )
    existing = _container(project="meeting-recorder-dev-test", ports={8010, 5910})

    errors = validate_dev_runtime(identity, [existing])

    assert any("already exists" in error for error in errors)


def test_guard_rejects_reserved_container_name_conflict():
    identity = RuntimeIdentity(
        project="meeting-recorder-dev-test",
        working_dir=r"C:\worktrees\test\meeting_recorder",
        image="meeting-recorder:dev-test",
        host_ports=frozenset({8001, 5901}),
        container_name="meeting-recorder",
    )

    errors = validate_dev_runtime(identity, [_container()])

    assert any("reserved container name" in error for error in errors)
    assert any("already used" in error for error in errors)


def test_guard_rejects_production_port_collision():
    identity = RuntimeIdentity(
        project="meeting-recorder-dev-test",
        working_dir=r"C:\worktrees\test\meeting_recorder",
        image="meeting-recorder:dev-test",
        host_ports=frozenset({8000, 5901}),
    )

    errors = validate_dev_runtime(identity, [_container(ports={8000, 5900})])

    assert any("Host port(s) 8000" in error for error in errors)


def test_guard_rejects_shared_image_tag_from_different_worktree():
    identity = RuntimeIdentity(
        project="meeting-recorder-dev-test",
        working_dir=r"C:\worktrees\test\meeting_recorder",
        image="meeting-recorder:dev",
        host_ports=frozenset({8001, 5901}),
    )

    errors = validate_dev_runtime(identity, [_container(image="meeting-recorder:dev", ports={8000, 5900})])

    assert any("Image tag 'meeting-recorder:dev'" in error for error in errors)


def test_guard_allows_isolated_project_ports_and_image():
    identity = RuntimeIdentity(
        project="meeting-recorder-dev-test",
        working_dir=r"C:\worktrees\test\meeting_recorder",
        image="meeting-recorder:dev-test",
        host_ports=frozenset({8001, 5901}),
    )

    errors = validate_dev_runtime(identity, [_container(ports={8000, 5900})])

    assert errors == []
