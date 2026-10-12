import json
from pathlib import Path

import pytest

from pylon import module_ci


REPO = "example/private-pylon"
REVISION = "a" * 40
RELEASE_ID = 7321
UPLOAD_URL = "https://uploads.example.invalid/repos/example/private-pylon/releases/7321/assets{?name,label}"


@pytest.fixture
def dispatch_fixture(monkeypatch, tmp_path):
    source = tmp_path / "modules"
    source.mkdir()
    calls = []
    bundle_calls = []

    monkeypatch.setattr(module_ci.subprocess, "check_output", lambda argv, **kwargs: (
        "" if argv[:3] == ["git", "status", "--porcelain"] else REVISION
    ))
    monkeypatch.setattr(module_ci.extensions, "registry", lambda path: {"demo": object()})

    def fake_bundle(_source, target):
        bundle_calls.append((Path(_source), Path(target)))
        target.write_bytes(b"fixture archive")
        return "c" * 64

    monkeypatch.setattr(module_ci, "bundle", fake_bundle)

    def fake_gh(*args, **kwargs):
        calls.append((args, kwargs))
        if args[:2] == ("repo", "view"):
            return json.dumps({"nameWithOwner": REPO, "isPrivate": True})
        if args[:4] == ("api", "--method", "POST", "repos/example/private-pylon/releases"):
            return json.dumps({"id": RELEASE_ID, "upload_url": UPLOAD_URL})
        return ""

    monkeypatch.setattr(module_ci, "gh", fake_gh)
    return source, calls, bundle_calls


def _call_args(calls):
    return [args for args, _kwargs in calls]


def test_dispatch_uses_creation_receipt_for_upload_and_workflow(dispatch_fixture, capsys):
    source, calls, bundle_calls = dispatch_fixture

    assert module_ci.dispatch(source, "demo", router=True) == 0

    call_args = _call_args(calls)
    create = next(args for args in call_args if args[:4] == ("api", "--method", "POST", "repos/example/private-pylon/releases"))
    upload = next(args for args in call_args if args[:4] == ("api", "--method", "POST", "https://uploads.example.invalid/repos/example/private-pylon/releases/7321/assets?name=source.tar.gz"))
    workflow = next(args for args in call_args if args[:2] == ("workflow", "run"))

    assert "-F" in create and "draft=true" in create
    assert upload[4:6] == ("--input", str(bundle_calls[0][1]))
    assert "-f" in workflow
    assert "release_id=7321" in workflow
    assert "router=true" in workflow
    assert not any(
        len(args) >= 2
        and args[0] == "api"
        and args[1].startswith("repos/example/private-pylon/releases/tags/")
        for args in call_args
    )

    result = json.loads(capsys.readouterr().out)
    assert result["temporary_release_id"] == RELEASE_ID


@pytest.mark.parametrize("failure_at", ["upload", "workflow"])
def test_dispatch_deletes_created_release_after_upload_or_workflow_failure(
    dispatch_fixture, monkeypatch, failure_at
):
    source, calls, _bundle_calls = dispatch_fixture

    def failing_gh(*args, **kwargs):
        calls.append((args, kwargs))
        if args[:2] == ("repo", "view"):
            return json.dumps({"nameWithOwner": REPO, "isPrivate": True})
        if args[:4] == ("api", "--method", "POST", "repos/example/private-pylon/releases"):
            return json.dumps({"id": RELEASE_ID, "upload_url": UPLOAD_URL})
        if failure_at == "upload" and args[0:2] == ("api", "--method") and "assets?name=source.tar.gz" in args[3]:
            raise RuntimeError("upload failed")
        if failure_at == "workflow" and args[:2] == ("workflow", "run"):
            raise RuntimeError("dispatch failed")
        return ""

    monkeypatch.setattr(module_ci, "gh", failing_gh)

    expected_error = "upload failed" if failure_at == "upload" else "dispatch failed"
    with pytest.raises(RuntimeError, match=expected_error):
        module_ci.dispatch(source, "demo")

    call_args = _call_args(calls)
    assert ("api", "--method", "DELETE", f"repos/{REPO}/releases/{RELEASE_ID}") in call_args
    if failure_at == "upload":
        assert not any(args[:2] == ("workflow", "run") for args in call_args)


def test_dispatch_rejects_public_repository_before_upload(monkeypatch, tmp_path):
    source = tmp_path / "modules"
    source.mkdir()
    calls = []

    monkeypatch.setattr(module_ci.subprocess, "check_output", lambda argv, **kwargs: (
        "" if argv[:3] == ["git", "status", "--porcelain"] else REVISION
    ))

    def fake_gh(*args, **kwargs):
        calls.append((args, kwargs))
        if args[:2] == ("repo", "view"):
            return json.dumps({"nameWithOwner": REPO, "isPrivate": False})
        raise AssertionError("public repository must be rejected before release creation")

    monkeypatch.setattr(module_ci, "gh", fake_gh)
    monkeypatch.setattr(module_ci, "bundle", lambda *_args: pytest.fail("public repository must not bundle"))

    with pytest.raises(ValueError, match="private repository"):
        module_ci.dispatch(source, "demo")

    assert [args for args, _kwargs in calls] == [("repo", "view", "--json", "nameWithOwner,isPrivate")]
