"""Unit tests for repo_scan: spec resolution, checkout/diff, inventory,
and text reading. Uses throwaway local git repos — no network."""

import os
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import config  # noqa: E402
import repo_scan  # noqa: E402
from repo_scan import RepoScanError  # noqa: E402


def _git(args, cwd):
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True,
        env={**os.environ,
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.fixture()
def fixture_repo(tmp_path):
    """A local repo with main + a bug-lab branch that changes app.py."""
    root = tmp_path / "origin"
    root.mkdir()
    _git(["init", "-b", "main"], root)
    (root / "app.py").write_text(
        "def search(q, items):\n"
        "    q = q.lower()\n"
        "    return [i for i in items if q in i.lower()]\n")
    (root / "README.md").write_text("# fixture\n")
    (root / "logo.png").write_bytes(b"\x89PNG\x00binary")
    _git(["add", "-A"], root)
    _git(["commit", "-m", "seed"], root)

    _git(["checkout", "-b", "bug-lab"], root)
    (root / "app.py").write_text(
        "def search(q, items):\n"
        "    return [i for i in items if q in i]\n")
    _git(["add", "-A"], root)
    _git(["commit", "-m", "seed regression: case-sensitive search"], root)
    _git(["checkout", "main"], root)
    return str(root)


def test_resolve_shorthand():
    assert repo_scan.resolve_repo_source("owner/name") == "https://github.com/owner/name.git"


def test_resolve_url_verbatim():
    url = "https://example.com/x/y.git"
    assert repo_scan.resolve_repo_source(url) == url


def test_resolve_local_path(tmp_path):
    assert repo_scan.resolve_repo_source(str(tmp_path)) == str(tmp_path)


def test_resolve_rejects_garbage():
    with pytest.raises(RepoScanError):
        repo_scan.resolve_repo_source("not a repo spec at all")
    with pytest.raises(RepoScanError):
        repo_scan.resolve_repo_source("")


def test_checkout_plain_branch(fixture_repo):
    co = repo_scan.checkout(fixture_repo, "main")
    try:
        assert not co.diff_mode
        assert len(co.head_sha) == 40
        files = repo_scan.list_files(co)
        paths = {f.path for f in files}
        assert "app.py" in paths
        assert "logo.png" not in paths  # binary suffix skipped
        assert repo_scan.read_text(co, "app.py").startswith("def search")
    finally:
        repo_scan.cleanup(co)
    assert not os.path.isdir(co.workdir)


def test_checkout_diff_mode(fixture_repo):
    co = repo_scan.checkout(fixture_repo, "bug-lab", base="main")
    try:
        assert co.diff_mode
        assert co.changed_files == ["app.py"]
        diff = co.diffs["app.py"]
        assert "-    q = q.lower()" in diff
        assert co.label().endswith("@bug-lab vs main")
    finally:
        repo_scan.cleanup(co)


def test_checkout_same_ref_rejected(fixture_repo):
    with pytest.raises(RepoScanError):
        repo_scan.checkout(fixture_repo, "main", base="main")


def test_checkout_missing_branch_errors(fixture_repo):
    with pytest.raises(RepoScanError):
        repo_scan.checkout(fixture_repo, "no-such-branch")


def test_read_text_binary_and_oversize(fixture_repo, monkeypatch):
    co = repo_scan.checkout(fixture_repo, "main")
    try:
        with open(os.path.join(co.workdir, "blob.bin"), "wb") as fh:
            fh.write(b"a\x00b")
        assert repo_scan.read_text(co, "blob.bin") is None
        monkeypatch.setattr(config, "REPO_MAX_FILE_BYTES", 2)
        assert repo_scan.read_text(co, "app.py") is None
    finally:
        repo_scan.cleanup(co)


def test_list_files_cap(fixture_repo, monkeypatch):
    monkeypatch.setattr(config, "REPO_MAX_FILES", 1)
    co = repo_scan.checkout(fixture_repo, "main")
    try:
        assert len(repo_scan.list_files(co)) == 1
    finally:
        repo_scan.cleanup(co)


def test_skippable_paths():
    assert repo_scan._skippable("node_modules/x/index.js")
    assert repo_scan._skippable("frontend/dist/app.min.js")
    assert repo_scan._skippable("package-lock.json")
    assert not repo_scan._skippable("src/app.ts")


def test_language_of():
    assert repo_scan.language_of("a/b.py") == "python"
    assert repo_scan.language_of("a/b.tsx") == "typescript"
    assert repo_scan.language_of("Makefile") == "other"
