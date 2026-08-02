"""Tier-0 source-code checks for the repo pipeline.

Deterministic, stdlib-only regex checks over a single file's text. Same
canonical finding-dict shape as the other tier0 modules, with `url` set to
the repo-relative file path so findings group per file, and `pipeline` set
to "code" (secret hits reuse the security scanner and stay "security").

These checks are deliberately few and defensible: each one flags a pattern
that is either always wrong (conflict markers, breakpoints) or a well-known
risk surface worth a look (shell=True, SQL built by string interpolation).
Nuance beyond that is the LLM tiers' job — ambiguous rules carry
``"ambiguous": True`` so triage decides whether deep review is warranted.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from tier0.security import scan_secrets

Finding = Dict[str, Any]

# (rule, pattern, languages or None for all, severity, title, detail, ambiguous)
_CHECKS = [
    (
        "code/conflict-marker",
        re.compile(r"^(<{7} |={7}$|>{7} )", re.M),
        None,
        "serious",
        "Unresolved merge conflict marker",
        "A git merge conflict marker was committed; the file cannot be "
        "correct as written.",
        False,
    ),
    (
        "code/breakpoint",
        re.compile(r"\b(pdb\.set_trace\(\)|breakpoint\(\)|debugger;)"),
        None,
        "moderate",
        "Debugger breakpoint left in source",
        "A debugging breakpoint statement is committed; it will halt or "
        "pollute execution in production.",
        False,
    ),
    (
        "code/eval",
        re.compile(r"(?<![\w.])(eval|exec)\s*\("),
        {"python", "javascript", "typescript"},
        "moderate",
        "Dynamic code evaluation",
        "eval/exec executes constructed code; verify the input can never be "
        "attacker-influenced.",
        True,
    ),
    (
        "code/subprocess-shell",
        re.compile(r"shell\s*=\s*True"),
        {"python"},
        "moderate",
        "subprocess with shell=True",
        "shell=True hands the command line to a shell; command injection "
        "risk if any argument is user-influenced.",
        True,
    ),
    (
        "code/pickle-load",
        re.compile(r"\bpickle\.loads?\s*\("),
        {"python"},
        "moderate",
        "Unsafe deserialization (pickle)",
        "Unpickling untrusted data executes arbitrary code; confirm the "
        "source is trusted.",
        True,
    ),
    (
        "code/yaml-unsafe-load",
        re.compile(r"\byaml\.load\s*\((?![^)]*Loader)"),
        {"python"},
        "moderate",
        "yaml.load without an explicit safe Loader",
        "yaml.load without Loader can instantiate arbitrary objects; use "
        "yaml.safe_load.",
        True,
    ),
    (
        "code/inner-html",
        re.compile(r"(\.innerHTML\s*=|dangerouslySetInnerHTML|document\.write\s*\()"),
        {"javascript", "typescript", "html"},
        "moderate",
        "Raw HTML injection surface",
        "Assigning markup strings into the DOM is an XSS surface; verify "
        "every interpolated value is escaped.",
        True,
    ),
    (
        "code/sql-interpolation",
        re.compile(
            r"""(?ix)
            (execute|executemany|query)\s*\(\s*
            (f["']|["'][^"']*["']\s*[%+]|.*\.format\()
            .*\b(select|insert|update|delete)\b
            """,
        ),
        {"python", "javascript", "typescript"},
        "serious",
        "SQL built by string interpolation",
        "The SQL text is assembled with interpolation/concatenation instead "
        "of bound parameters; injection risk if any value is user input.",
        True,
    ),
    (
        "code/tls-verify-disabled",
        re.compile(r"(verify\s*=\s*False|rejectUnauthorized\s*:\s*false)"),
        None,
        "serious",
        "TLS certificate verification disabled",
        "Disabling certificate verification allows trivial "
        "man-in-the-middle interception.",
        False,
    ),
    (
        "code/bare-except",
        re.compile(r"^\s*except\s*:\s*(#.*)?$", re.M),
        {"python"},
        "minor",
        "Bare except swallows all errors",
        "A bare `except:` catches SystemExit/KeyboardInterrupt and hides "
        "real failures; catch specific exceptions.",
        True,
    ),
]

_TODO_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _snippet(text: str, offset: int, limit: int = 160) -> str:
    start = text.rfind("\n", 0, offset) + 1
    end = text.find("\n", offset)
    if end == -1:
        end = len(text)
    return text[start:end].strip()[:limit]


def _finding(path: str, rule: str, severity: str, title: str, detail: str,
             evidence: Dict[str, Any], ambiguous: bool) -> Finding:
    f: Finding = {
        "url": path,
        "pipeline": "code",
        "tier": 0,
        "rule": rule,
        "severity": severity,
        "title": title,
        "detail": detail,
        "evidence": evidence,
    }
    if ambiguous:
        f["ambiguous"] = True
    return f


def run_repo_checks(path: str, text: str, language: str) -> List[Finding]:
    """All Tier-0 checks for one file. Each rule reports at most its first
    3 hits per file (enough to locate the problem without finding spam)."""
    findings: List[Finding] = []
    if not text:
        return findings

    for rule, pattern, languages, severity, title, detail, ambiguous in _CHECKS:
        if languages is not None and language not in languages:
            continue
        for hits, m in enumerate(pattern.finditer(text)):
            if hits >= 3:
                break
            findings.append(_finding(
                path, rule, severity, title, detail,
                {
                    "source": path,
                    "line": _line_of(text, m.start()),
                    "snippet": _snippet(text, m.start()),
                },
                ambiguous,
            ))

    todo_count = len(_TODO_RE.findall(text))
    if todo_count:
        findings.append(_finding(
            path, "code/todo-comments", "info",
            f"{todo_count} TODO/FIXME marker(s)",
            "Leftover work markers; worth reviewing whether any hide "
            "unfinished behavior.",
            {"source": path, "count": str(todo_count)},
            False,
        ))

    # Secret scan reuses the security-tier patterns/redaction verbatim;
    # per-file call so findings anchor to the file path.
    findings.extend(scan_secrets(path, {path: text}))
    return findings
