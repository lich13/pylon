import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest

from pylon import command_catalog, extensions, module_store, module_units, runner
from pylon.config import jobs


def isolate(monkeypatch, tmp_path):
    home = tmp_path / "home"
    source = tmp_path / "modules"
    home.mkdir()
    source.mkdir()
    for profile in ("default", "webops"):
        (home / "profiles" / profile).mkdir(parents=True)
    (home / "jobs.json").write_text("{}")
    monkeypatch.setenv("PYLON_HOME", str(home))
    monkeypatch.setenv("PYLON_MODULES_DIR", str(source))
    monkeypatch.setenv("PYLON_TASK_PYTHON", sys.executable)
    monkeypatch.delenv("PYLON_MODULE_SNAPSHOT", raising=False)
    return home, source


def write_module(root, *, module_id="demo", version="one", health="ok", api_version=1,
                 packages=None, handler="pylon_tasks.demo.handler", extra=None,
                 job=None, commands=None, callbacks=None, cli=None):
    root.mkdir(parents=True, exist_ok=True)
    package_name = "demo" if module_id == "demo" else module_id
    packages = packages if packages is not None else [f"pylon_tasks.{package_name}"]
    manifest = {
        "id": module_id,
        "api_version": api_version,
        "data_version": 1,
        "handler": handler,
        "packages": packages,
    }
    if job is not None:
        manifest["jobs"] = {"module-job": job}
    if commands is not None:
        manifest["commands"] = commands
    if callbacks is not None:
        manifest["callbacks"] = callbacks
    if cli is not None:
        manifest["cli"] = cli
    if extra:
        manifest.update(extra)
    (root / "module.json").write_text(json.dumps(manifest))
    namespace = root / "src" / "pylon_tasks" / package_name
    namespace.mkdir(parents=True, exist_ok=True)
    (namespace / "handler.py").write_text(
        "from pylon.sdk import serve\n"
        "\n"
        "def handle(action, profile, payload):\n"
        f"    if action == 'health': return {{'status': {health!r}}}\n"
        f"    return {{'version': {version!r}, 'action': action, 'profile': profile, 'payload': payload}}\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    serve(handle)\n"
    )
    return root


def valid_job(module="pylon_tasks.demo.handler", timeout=None):
    return {
        "name": "fixture module job",
        "module": module,
        "profile": "webops",
        "schedule": "*/5 * * * *",
        "lock": "fixture-module",
        "timeout": timeout,
    }


def test_core_catalog_and_cli_work_without_external_modules(monkeypatch, tmp_path, capsys):
    _home, source = isolate(monkeypatch, tmp_path)
    assert extensions.registry(source) == {}
    assert jobs() == {}
    assert command_catalog.catalog() == command_catalog.CORE_COMMANDS
    assert command_catalog.menu_commands("default")

    from pylon.module_cli import main

    assert main(["--source", str(source), "list"]) == 0
    assert json.loads(capsys.readouterr().out) == {}


@pytest.mark.parametrize("bad_api", [0, 2, True])
def test_incompatible_module_protocol_is_rejected(monkeypatch, tmp_path, bad_api):
    _home, source = isolate(monkeypatch, tmp_path)
    write_module(source / "demo", api_version=bad_api)
    with pytest.raises(ValueError, match="incompatible module API"):
        extensions.registry(source)


@pytest.mark.parametrize("kind", ["jobs", "commands", "packages"])
def test_registry_rejects_duplicate_registrations(monkeypatch, tmp_path, kind):
    _home, source = isolate(monkeypatch, tmp_path)
    first = {}
    second = {}
    if kind == "jobs":
        first["job"] = valid_job()
        second["job"] = valid_job(module="pylon_tasks.other.handler")
    elif kind == "commands":
        item = {"name": "same_command", "description": "fixture", "route": "fixture",
                "profiles": ["default"]}
        first["commands"] = [item]
        second["commands"] = [item]
    else:
        first["packages"] = ["pylon_tasks.demo", "pylon_tasks.shared"]
        second["packages"] = ["pylon_tasks.other", "pylon_tasks.shared"]
    write_module(source / "demo", **first)
    write_module(source / "other", module_id="other", handler="pylon_tasks.other.handler", **second)
    with pytest.raises(ValueError, match=f"duplicate {kind}"):
        extensions.registry(source)


@pytest.mark.parametrize("field", ["admin_action", "argv", "python"])
def test_external_job_cannot_request_privileged_or_arbitrary_execution(monkeypatch, tmp_path, field):
    _home, source = isolate(monkeypatch, tmp_path)
    job = valid_job()
    job[field] = "ssh travel open" if field == "admin_action" else ["/bin/sh"]
    write_module(source / "demo", job=job)
    with pytest.raises(ValueError, match="privileged execution"):
        extensions.registry(source)


def test_entrypoint_and_package_paths_are_confined_to_controlled_namespace(monkeypatch, tmp_path):
    _home, source = isolate(monkeypatch, tmp_path)
    write_module(source / "demo", packages=["pylon_tasks.demo"], handler="pylon.admin.control")
    with pytest.raises(ValueError, match="entrypoint is outside declared packages"):
        extensions.registry(source)

    write_module(source / "bad", module_id="bad", packages=["pylon_tasks.demo", "../escape"])
    with pytest.raises(ValueError, match="invalid module packages"):
        extensions.registry(source)


def test_unpack_rejects_path_traversal_and_absolute_entries(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    for name in ("../escape", "/absolute"):
        archive = tmp_path / (name.replace("/", "_") + ".tar")
        with tarfile.open(archive, "w") as bundle:
            item = tarfile.TarInfo(name)
            item.size = 1
            bundle.addfile(item, __import__("io").BytesIO(b"x"))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        with pytest.raises(ValueError, match="unsafe"):
            module_store.unpack(archive, target, digest)


def test_sdk_handler_runs_as_real_json_subprocess(monkeypatch, tmp_path):
    _home, source = isolate(monkeypatch, tmp_path)
    write_module(source / "demo", version="fixture-v1")
    assert extensions.invoke("demo", "echo", "webops", value="from-fixture") == {
        "version": "fixture-v1",
        "action": "echo",
        "profile": "webops",
        "payload": {"value": "from-fixture"},
    }

    env = extensions.environment("webops", extensions.registry(source))
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "pylon_tasks.demo.handler"],
        input=json.dumps({"api_version": 99, "action": "echo", "profile": "webops", "payload": {}}),
        cwd=source / "demo", env=env, text=True, capture_output=True,
    )
    assert proc.returncode != 0
    assert "unsupported extension request" in proc.stderr


def test_runner_uses_fixed_python_no_timeout_and_does_not_resubmit(monkeypatch, tmp_path):
    home, source = isolate(monkeypatch, tmp_path)
    write_module(source / "demo", job=valid_job(timeout=None))
    modules = extensions.registry(source)
    calls = []

    class Process:
        pid = 4242
        returncode = 0

        def communicate(self, timeout=None):
            calls.append(timeout)
            result_path = Path(os.environ["PYLON_RESULT_FILE"])
            result_path.write_text(json.dumps({"status": "ok", "changed": False}))
            return "", ""

        def poll(self):
            return self.returncode

    def start(argv, **kwargs):
        calls.append((argv, kwargs))
        os.environ["PYLON_RESULT_FILE"] = kwargs["env"]["PYLON_RESULT_FILE"]
        return Process()

    monkeypatch.setattr(runner.subprocess, "Popen", start)
    monkeypatch.setenv("INVOCATION_ID", "fixed-invocation")
    assert runner._run("module-job", "manual", modules) == 0
    assert runner._run("module-job", "manual", modules) == 0
    assert calls[0][0] == [sys.executable, "-m", "pylon_tasks.demo.handler"]
    assert calls[1] is None
    assert len([item for item in calls if isinstance(item, tuple)]) == 1
    assert (home / "run" / "fixture-module.lock").exists()


def test_analysis_job_unit_allows_unbounded_wait_only_when_explicit(monkeypatch):
    assert "TimeoutStartSec=infinity" in module_units.analysis_override({"timeout": None})
    assert module_units.analysis_override({"timeout": 30}) == ""


def test_pack_install_atomic_pointer_rollback_and_failures(monkeypatch, tmp_path):
    _home, source = isolate(monkeypatch, tmp_path)
    destination = tmp_path / "installed"
    v1 = write_module(tmp_path / "v1" / "demo", version="v1")
    v2 = write_module(tmp_path / "v2" / "demo", version="v2")
    bad_health = write_module(tmp_path / "bad" / "demo", version="bad", health="broken")
    a1 = tmp_path / "v1.tar.gz"
    a2 = tmp_path / "v2.tar.gz"
    ab = tmp_path / "bad.tar.gz"
    r1 = module_store.pack(v1, a1)
    r2 = module_store.pack(v2, a2)
    rb = module_store.pack(bad_health, ab)

    first = module_store.install([(a1, r1["archive_sha256"])], destination, services=False)
    index1 = module_store.index(destination)
    assert first["restarted"] == []
    assert index1["previous"] == {}
    release1 = index1["modules"]["demo"]

    second = module_store.install([(a2, r2["archive_sha256"])], destination, services=False)
    index2 = module_store.index(destination)
    assert second["restarted"] == []
    assert index2["modules"]["demo"] != release1
    assert index2["previous"]["demo"] == release1
    assert extensions.registry(destination)["demo"].root == (destination / index2["modules"]["demo"]).resolve()

    rolled = module_store.rollback("demo", destination, services=False)
    index3 = module_store.index(destination)
    assert rolled["restarted"] == []
    assert index3["modules"]["demo"] == release1
    assert index3["previous"]["demo"] == index2["modules"]["demo"]

    before = json.loads((destination / "registry.json").read_text())
    with pytest.raises(ValueError, match="checksum mismatch"):
        module_store.install([(a2, "0" * 64)], destination, services=False)
    assert json.loads((destination / "registry.json").read_text()) == before

    with pytest.raises(ValueError, match="health check failed"):
        module_store.install([(ab, rb["archive_sha256"])], destination, services=False)
    assert json.loads((destination / "registry.json").read_text()) == before


def test_lease_keeps_unreferenced_release_until_unlock_then_prune(monkeypatch, tmp_path):
    _home, source = isolate(monkeypatch, tmp_path)
    destination = tmp_path / "installed"
    v1 = write_module(tmp_path / "v1" / "demo", version="v1")
    v2 = write_module(tmp_path / "v2" / "demo", version="v2")
    a1 = tmp_path / "v1.tar.gz"
    r1 = module_store.pack(v1, a1)
    module_store.install([(a1, r1["archive_sha256"])], destination, services=False)
    old_rel = destination / module_store.index(destination)["modules"]["demo"]

    digest2 = module_store.content_hash(v2)
    new_rel = destination / "releases" / "demo" / digest2
    new_rel.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(v2, new_rel)
    module_store.atomic_json(destination / "registry.json", {
        "api_version": 1,
        "modules": {"demo": new_rel.relative_to(destination).as_posix()},
        "previous": {},
    })

    modules = {"demo": extensions.Module("demo", old_rel, json.loads((old_rel / "module.json").read_text()))}
    with extensions.lease(modules):
        module_store.prune(destination)
        assert old_rel.exists()
    module_store.prune(destination)
    assert not old_rel.exists()
