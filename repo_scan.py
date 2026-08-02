"""Git plumbing for the repo pipeline: resolve a repo spec, clone a branch
into a temp workdir, optionally diff it against a base branch, and inventory
the tracked files.

Deliberately subprocess-git (no GitPython dependency) and read-only: the
engine never executes anything from the scanned repo — it only clones and
reads text. Supports three repo spec shapes:

    owner/name          -> https://github.com/owner/name.git
    https://host/...    -> used verbatim
    /local/path         -> cloned from the local filesystem (tests, mirrors)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import config

_SHORTHAND_RE = re.compile(r"^[\w.-]+/[\w.-]+$")

# Tracked files matching any of these are never scanned or sent to a model:
# vendored/minified/lock/binary content wastes tokens and drowns findings.
_SKIP_DIR_PARTS = {
    "node_modules", "vendor", "dist", "build", ".venv", "venv",
    "__pycache__", ".git",
}
_SKIP_SUFFIXES = (
    ".min.js", ".min.css", ".map", ".lock",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg",
    ".woff", ".woff2", ".ttf", ".eot", ".pdf", ".zip", ".gz",
    ".sqlite3", ".db", ".pyc", ".ipynb",
)
_SKIP_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock"}

_EXT_LANGUAGE = {
    ".py": "python", ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".html": "html", ".css": "css", ".json": "json",
    ".md": "markdown", ".yml": "yaml", ".yaml": "yaml", ".sh": "shell",
    ".sql": "sql", ".go": "go", ".rs": "rust", ".rb": "ruby",
    ".java": "java", ".toml": "toml", ".cfg": "config", ".ini": "config",
}


class RepoScanError(RuntimeError):
    """A git operation failed; message is safe to surface to the client."""


@dataclass
class RepoFile:
    path: str          # repo-relative, forward slashes
    size: int
    language: str


@dataclass
class Checkout:
    repo: str                       # the spec the caller passed
    branch: str
    workdir: str                    # absolute path of the clone
    head_sha: str
    base: Optional[str] = None
    base_sha: Optional[str] = None
    changed_files: Optional[List[str]] = None   # diff mode only
    diffs: Dict[str, str] = field(default_factory=dict)  # path -> unified diff

    @property
    def diff_mode(self) -> bool:
        return self.base is not None

    def label(self) -> str:
        if self.diff_mode:
            return f"{self.repo}@{self.branch} vs {self.base}"
        return f"{self.repo}@{self.branch}"


def resolve_repo_source(repo: str) -> str:
    """Map a repo spec to something `git clone` accepts."""
    repo = (repo or "").strip()
    if not repo:
        raise RepoScanError("empty repo spec")
    if repo.startswith(("https://", "http://", "git@", "ssh://", "file://")):
        return repo
    if os.path.isdir(repo):
        return repo
    if _SHORTHAND_RE.match(repo):
        return f"https://github.com/{repo}.git"
    raise RepoScanError(
        f"unrecognized repo spec {repo!r} — use owner/name, an https URL, "
        "or a local path")


def _git(args: List[str], cwd: Optional[str] = None) -> str:
    """Run a git command, returning stdout. Raises RepoScanError with a
    stderr excerpt on failure (never leaks credentials — none are passed)."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            timeout=config.REPO_GIT_TIMEOUT_SECS,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired:
        raise RepoScanError(f"git {args[0]} timed out")
    except FileNotFoundError:
        raise RepoScanError("git binary not found on PATH")
    if proc.returncode != 0:
        excerpt = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RepoScanError(
            f"git {args[0]} failed: {excerpt[-1] if excerpt else 'unknown error'}")
    return proc.stdout


def checkout(repo: str, branch: str, base: Optional[str] = None) -> Checkout:
    """Clone `repo` at `branch` into a fresh temp dir. When `base` is given,
    also fetch it and record the merge-base diff (changed files + per-file
    unified diffs). Caller owns cleanup (see `cleanup`)."""
    source = resolve_repo_source(repo)
    branch = (branch or "").strip() or "main"
    base = (base or "").strip() or None
    if base == branch:
        raise RepoScanError("base and branch are the same ref — nothing to diff")

    workdir = tempfile.mkdtemp(prefix="qa-repo-")
    try:
        _git(["clone", "--no-tags", "--single-branch", "--branch", branch,
              source, workdir])
        head_sha = _git(["rev-parse", "HEAD"], cwd=workdir).strip()

        base_sha = None
        changed: Optional[List[str]] = None
        diffs: Dict[str, str] = {}
        if base:
            _git(["fetch", "--no-tags", "origin", base], cwd=workdir)
            fetched = _git(["rev-parse", "FETCH_HEAD"], cwd=workdir).strip()
            merge_base = _git(["merge-base", fetched, "HEAD"], cwd=workdir).strip()
            base_sha = merge_base
            names = _git(["diff", "--name-only", merge_base, "HEAD"],
                         cwd=workdir)
            changed = [n for n in names.splitlines() if n.strip()]
            for path in changed:
                diffs[path] = _git(
                    ["diff", merge_base, "HEAD", "--", path], cwd=workdir)

        return Checkout(
            repo=repo, branch=branch, workdir=workdir, head_sha=head_sha,
            base=base, base_sha=base_sha, changed_files=changed, diffs=diffs)
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        raise


def cleanup(co: Checkout) -> None:
    shutil.rmtree(co.workdir, ignore_errors=True)


def _skippable(path: str) -> bool:
    parts = path.split("/")
    if any(p in _SKIP_DIR_PARTS for p in parts[:-1]):
        return True
    name = parts[-1]
    if name in _SKIP_NAMES:
        return True
    return name.lower().endswith(_SKIP_SUFFIXES)


def language_of(path: str) -> str:
    _, ext = os.path.splitext(path)
    return _EXT_LANGUAGE.get(ext.lower(), "other")


def list_files(co: Checkout) -> List[RepoFile]:
    """Inventory the clone's tracked files (git ls-files honors .gitignore by
    construction), dropping vendored/binary noise and capping the count."""
    out: List[RepoFile] = []
    names = _git(["ls-files"], cwd=co.workdir).splitlines()
    for path in names:
        path = path.strip()
        if not path or _skippable(path):
            continue
        full = os.path.join(co.workdir, path)
        try:
            size = os.path.getsize(full)
        except OSError:
            continue
        out.append(RepoFile(path=path, size=size, language=language_of(path)))
        if len(out) >= config.REPO_MAX_FILES:
            break
    return out


def read_text(co: Checkout, path: str) -> Optional[str]:
    """Read a repo file as UTF-8 text. Returns None for binary or oversized
    files (they are inventoried but never scanned/sent to a model)."""
    full = os.path.join(co.workdir, path)
    try:
        if os.path.getsize(full) > config.REPO_MAX_FILE_BYTES:
            return None
        with open(full, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    if b"\x00" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def file_tree_summary(files: List[RepoFile]) -> str:
    """Compact one-line-per-file inventory for the triage prompt."""
    return "\n".join(f"{f.path}  ({f.language}, {f.size}B)" for f in files)
