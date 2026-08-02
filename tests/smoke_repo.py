"""End-to-end repo-pipeline smoke test (keyless / MOCK_MODELS).

Builds a throwaway local git repo with seeded problems on a `bug-lab`
branch, runs the SSE repo pipeline against it in diff mode, and asserts:
the clone+diff happened, Tier-0 source findings streamed (conflict marker,
breakpoint, secret), stage order is sane, the run row landed in the DB, and
the temp clone was cleaned up. Run:

    MOCK_MODELS=1 .venv/bin/python -m tests.smoke_repo
"""

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MOCK_MODELS", "1")

import config  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _git(args, cwd):
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True,
        env={**os.environ,
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def _build_fixture_repo(root):
    _git(["init", "-b", "main"], root)
    with open(os.path.join(root, "app.py"), "w") as fh:
        fh.write(
            "def login(password, expected):\n"
            "    return password == expected\n"
            "\n"
            "def search(q, items):\n"
            "    q = q.lower()\n"
            "    return [i for i in items if q in i.lower()]\n")
    with open(os.path.join(root, "README.md"), "w") as fh:
        fh.write("# smoke fixture\n")
    _git(["add", "-A"], root)
    _git(["commit", "-m", "clean seed"], root)

    _git(["checkout", "-b", "bug-lab"], root)
    with open(os.path.join(root, "app.py"), "w") as fh:
        fh.write(
            "import pdb\n"
            "\n"
            'API_SECRET = "AKIAABCDEFGHIJKLMNOP"\n'
            "\n"
            "def login(password, expected):\n"
            "    pdb.set_trace()\n"
            "    return True  # regression: any password accepted\n"
            "\n"
            "def search(q, items):\n"
            "<<<<<<< HEAD\n"
            "    return [i for i in items if q in i]\n"
            "=======\n"
            "    return [i for i in items if q in i.lower()]\n"
            ">>>>>>> main\n")
    _git(["add", "-A"], root)
    _git(["commit", "-m", "seed regressions"], root)
    _git(["checkout", "main"], root)


def _collect(frame_gen):
    events = []
    for frame in frame_gen:
        if frame.startswith(":"):
            continue
        etype = data = None
        for line in frame.splitlines():
            if line.startswith("event:"):
                etype = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
        if etype:
            events.append((etype, json.loads(data) if data else {}))
    return events


def main():
    # fresh db per smoke run
    config.DB_PATH = os.path.join(FIXTURES, "_smoke_repo.db")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(config.DB_PATH + suffix)
        except FileNotFoundError:
            pass

    import db
    db.reset()
    db.init_db()

    from pipelines.repo import sse_repo

    pre_existing = {d for d in os.listdir(tempfile.gettempdir())
                    if d.startswith("qa-repo-")}
    with tempfile.TemporaryDirectory(prefix="qa-smoke-repo-") as root:
        _build_fixture_repo(root)
        events = _collect(sse_repo(root, "bug-lab", "main", {}))

    by_type = {}
    for etype, data in events:
        by_type.setdefault(etype, []).append(data)

    # -- frame order and stages
    assert events[0][0] == "run_start", events[0]
    assert events[-1][0] == "done", events[-1]
    stages = [(d["stage"], d["status"]) for d in by_type.get("stage", [])]
    for expected in [("fetch", "start"), ("fetch", "end"),
                     ("scan", "start"), ("scan", "end"),
                     ("triage", "start"), ("triage", "end")]:
        assert expected in stages, f"missing stage {expected}: {stages}"

    # -- diff mode found exactly the changed file
    fetch_end = next(d for d in by_type["stage"]
                     if d["stage"] == "fetch" and d["status"] == "end")
    assert "1 changed vs main" in fetch_end["note"], fetch_end

    # -- tier-0 findings: conflict marker, breakpoint, secret (redacted)
    findings = by_type.get("finding", [])
    rules = {f["rule"] for f in findings}
    assert "code/conflict-marker" in rules, rules
    assert "code/breakpoint" in rules, rules
    assert "aws-access-key" in rules, rules
    assert all(f["url"] == "app.py" for f in findings), findings
    assert "AKIAABCDEFGHIJKLMNOP" not in json.dumps(findings)

    # -- progress events cover only the changed file
    progress = by_type.get("page_progress", [])
    assert progress and all(p["url"] == "app.py" for p in progress)

    # -- done stats + db row
    done = events[-1][1]
    assert done["status"] == "done", done
    assert done["stats"]["changed_files"] == 1
    assert done["stats"]["pages"] == 1

    summary = db.run_summary(done["run_id"])
    assert summary["status"] == "done"
    assert summary["kind"] == "repo"
    assert db.run_findings(done["run_id"])

    # -- the temp clone was cleaned up (ignore clones from other processes)
    leftovers = [d for d in os.listdir(tempfile.gettempdir())
                 if d.startswith("qa-repo-") and d not in pre_existing]
    assert not leftovers, leftovers

    db.close()
    print(f"OK — {len(findings)} tier-0 findings, "
          f"stats: {json.dumps(done['stats']['findings_by_severity'])}")


if __name__ == "__main__":
    main()
