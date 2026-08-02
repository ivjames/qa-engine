"""Unit tests for tier0/repo.py: each deterministic check fires on a
minimal positive sample and stays quiet on a clean file."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tier0.repo import run_repo_checks  # noqa: E402


def rules_of(findings):
    return {f["rule"] for f in findings}


def test_clean_file_is_quiet():
    text = "def add(a, b):\n    return a + b\n"
    assert run_repo_checks("clean.py", text, "python") == []


def test_conflict_marker():
    text = "x = 1\n<<<<<<< HEAD\ny = 2\n=======\ny = 3\n>>>>>>> branch\n"
    findings = run_repo_checks("a.py", text, "python")
    assert "code/conflict-marker" in rules_of(findings)
    f = next(f for f in findings if f["rule"] == "code/conflict-marker")
    assert f["severity"] == "serious"
    assert f["url"] == "a.py"
    assert f["evidence"]["line"] == 2


def test_breakpoints():
    assert "code/breakpoint" in rules_of(
        run_repo_checks("a.py", "import pdb\npdb.set_trace()\n", "python"))
    assert "code/breakpoint" in rules_of(
        run_repo_checks("a.js", "debugger;\n", "javascript"))


def test_eval_language_gated():
    assert "code/eval" in rules_of(
        run_repo_checks("a.js", "eval(userInput)\n", "javascript"))
    # method calls and other languages don't fire
    assert "code/eval" not in rules_of(
        run_repo_checks("a.js", "model.eval()\n", "javascript"))
    assert "code/eval" not in rules_of(
        run_repo_checks("a.md", "eval(x) in prose\n", "markdown"))


def test_python_specific_checks():
    text = (
        "import subprocess, pickle, yaml\n"
        "subprocess.run(cmd, shell=True)\n"
        "pickle.loads(blob)\n"
        "yaml.load(doc)\n"
        "try:\n    pass\nexcept:\n    pass\n"
    )
    rules = rules_of(run_repo_checks("a.py", text, "python"))
    assert {"code/subprocess-shell", "code/pickle-load",
            "code/yaml-unsafe-load", "code/bare-except"} <= rules


def test_yaml_safe_load_ok():
    assert "code/yaml-unsafe-load" not in rules_of(run_repo_checks(
        "a.py", "yaml.load(doc, Loader=yaml.SafeLoader)\n", "python"))


def test_inner_html_ts_only():
    text = "el.innerHTML = userValue;\n"
    assert "code/inner-html" in rules_of(
        run_repo_checks("a.ts", text, "typescript"))
    assert "code/inner-html" not in rules_of(
        run_repo_checks("a.py", text, "python"))


def test_sql_interpolation():
    text = 'cur.execute(f"SELECT * FROM t WHERE id = {user_id}")\n'
    findings = run_repo_checks("a.py", text, "python")
    assert "code/sql-interpolation" in rules_of(findings)
    # bound parameters stay quiet
    ok = 'cur.execute("SELECT * FROM t WHERE id = ?", (user_id,))\n'
    assert "code/sql-interpolation" not in rules_of(
        run_repo_checks("a.py", ok, "python"))


def test_tls_verify_disabled():
    assert "code/tls-verify-disabled" in rules_of(run_repo_checks(
        "a.py", "requests.get(url, verify=False)\n", "python"))
    assert "code/tls-verify-disabled" in rules_of(run_repo_checks(
        "a.js", "https.request({rejectUnauthorized: false})\n", "javascript"))


def test_todo_aggregated_once():
    text = "# TODO one\n# FIXME two\n# HACK three\n"
    findings = run_repo_checks("a.py", text, "python")
    todo = [f for f in findings if f["rule"] == "code/todo-comments"]
    assert len(todo) == 1
    assert todo[0]["severity"] == "info"
    assert "3" in todo[0]["title"]


def test_secret_scan_included_and_redacted():
    text = 'AWS_KEY = "AKIAABCDEFGHIJKLMNOP"\n'
    findings = run_repo_checks("conf.py", text, "python")
    aws = [f for f in findings if f["rule"] == "aws-access-key"]
    assert aws and aws[0]["pipeline"] == "security"
    assert "AKIAABCDEFGHIJKLMNOP" not in str(aws[0])


def test_per_rule_hit_cap():
    text = "debugger;\n" * 10
    findings = run_repo_checks("a.js", text, "javascript")
    assert len([f for f in findings if f["rule"] == "code/breakpoint"]) == 3
