"""Tier-0 security checks.

Deterministic, stdlib-only checks that run over an already-crawled page:
response headers, TLS/certificate metadata (as surfaced by Playwright's
``response.security_details()``), and page/script text for hard-coded
secrets. No network calls, no browser — pure functions over data the
caller already collected.

Every check returns a list of canonical finding dicts::

    {
        "url": str,
        "pipeline": "security",
        "tier": 0,
        "rule": str,
        "severity": "info" | "minor" | "moderate" | "serious" | "critical",
        "title": str,
        "detail": str,
        "evidence": dict,
    }

Findings that are ambiguous enough to deserve LLM triage (tier-1+) also
carry ``"ambiguous": True``.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple


Finding = Dict[str, Any]


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------

def _finding(
    url: str,
    rule: str,
    severity: str,
    title: str,
    detail: str,
    evidence: Dict[str, Any],
    ambiguous: bool = False,
) -> Finding:
    f: Finding = {
        "url": url,
        "pipeline": "security",
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


def _is_https(url: str) -> bool:
    return url.strip().lower().startswith("https://")


# ---------------------------------------------------------------------------
# check_headers
# ---------------------------------------------------------------------------

def _parse_csp(csp: str) -> Dict[str, List[str]]:
    """Split a CSP header value into {directive-name: [source tokens]}.

    Directive names and source tokens are lowercased for easy matching;
    callers that need the original casing should re-inspect the raw
    header value (kept in evidence).
    """
    directives: Dict[str, List[str]] = {}
    for part in csp.split(";"):
        tokens = part.strip().split()
        if not tokens:
            continue
        name = tokens[0].lower()
        values = [t.lower() for t in tokens[1:]]
        directives[name] = values
    return directives


def _parse_max_age(hsts_value: str) -> Optional[int]:
    m = re.search(r"max-age\s*=\s*(\d+)", hsts_value, re.IGNORECASE)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _iter_cookies(value: Any) -> List[str]:
    """Normalize a Set-Cookie header value into a list of per-cookie strings.

    Accepts a single cookie string, a newline-joined string (how several
    HTTP libraries represent repeated headers), or a list/tuple of
    strings (one per Set-Cookie header).
    """
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v]
    if isinstance(value, str):
        if "\n" in value:
            return [c for c in value.split("\n") if c.strip()]
        return [value]
    return []


def _check_cookie(cookie_str: str) -> Tuple[str, List[str]]:
    """Return (cookie-name, [missing security attribute names])."""
    parts = [p.strip() for p in cookie_str.split(";") if p.strip()]
    first = parts[0] if parts else ""
    name = first.split("=", 1)[0].strip() if "=" in first else first.strip()
    attrs = [p.lower() for p in parts[1:]]

    missing: List[str] = []
    if not any(a == "secure" for a in attrs):
        missing.append("Secure")
    if not any(a == "httponly" for a in attrs):
        missing.append("HttpOnly")
    if not any(a.startswith("samesite") for a in attrs):
        missing.append("SameSite")
    return name, missing


def check_headers(url: str, headers: Dict[str, str]) -> List[Finding]:
    """Evaluate response headers for common security misconfigurations.

    ``headers`` keys are expected to already be lowercase; this function
    is defensive and lowercases anyway.
    """
    h = {str(k).lower(): v for k, v in (headers or {}).items()}
    findings: List[Finding] = []
    https = _is_https(url)

    # --- Content-Security-Policy ---
    csp = h.get("content-security-policy")
    csp_directives: Dict[str, List[str]] = {}
    if not csp:
        findings.append(
            _finding(
                url,
                "missing-csp",
                "serious",
                "Missing Content-Security-Policy header",
                "No Content-Security-Policy header was present, leaving the "
                "page without a script-injection safety net.",
                {"header": "content-security-policy", "present": False},
            )
        )
    else:
        csp_directives = _parse_csp(csp)
        weak_in = [
            name
            for name in ("script-src", "default-src")
            if any(
                v in ("'unsafe-inline'", "'unsafe-eval'")
                for v in csp_directives.get(name, [])
            )
        ]
        if weak_in:
            findings.append(
                _finding(
                    url,
                    "weak-csp",
                    "moderate",
                    "Content-Security-Policy allows unsafe inline/eval",
                    "The CSP's "
                    + " / ".join(weak_in)
                    + " directive permits 'unsafe-inline' or 'unsafe-eval', "
                    "which significantly weakens its XSS protection.",
                    {
                        "header": "content-security-policy",
                        "value": csp,
                        "weak_directives": weak_in,
                    },
                    ambiguous=True,
                )
            )

    # --- HSTS ---
    hsts = h.get("strict-transport-security")
    if https and not hsts:
        findings.append(
            _finding(
                url,
                "missing-hsts",
                "serious",
                "Missing Strict-Transport-Security header",
                "This HTTPS page does not send Strict-Transport-Security, "
                "so browsers won't enforce HTTPS on subsequent visits.",
                {"header": "strict-transport-security", "present": False},
            )
        )
    elif hsts:
        max_age = _parse_max_age(hsts)
        if max_age is not None and max_age < 15552000:
            findings.append(
                _finding(
                    url,
                    "short-hsts",
                    "moderate",
                    "Strict-Transport-Security max-age is too short",
                    f"max-age={max_age} is below the recommended minimum of "
                    "15552000 seconds (180 days).",
                    {
                        "header": "strict-transport-security",
                        "value": hsts,
                        "max_age": max_age,
                    },
                )
            )

    # --- Clickjacking protection ---
    xfo = h.get("x-frame-options")
    frame_ancestors = "frame-ancestors" in csp_directives
    if not xfo and not frame_ancestors:
        findings.append(
            _finding(
                url,
                "missing-frame-protection",
                "moderate",
                "Missing clickjacking protection",
                "Neither X-Frame-Options nor a CSP frame-ancestors "
                "directive is present, so the page can be framed by "
                "another site.",
                {"x-frame-options": xfo, "csp_frame_ancestors": frame_ancestors},
            )
        )

    # --- X-Content-Type-Options ---
    xcto = h.get("x-content-type-options")
    if not xcto or "nosniff" not in xcto.lower():
        findings.append(
            _finding(
                url,
                "missing-xcto",
                "minor",
                "Missing X-Content-Type-Options: nosniff",
                "Without 'nosniff', browsers may MIME-sniff responses in "
                "ways that enable content-type confusion attacks.",
                {"header": "x-content-type-options", "value": xcto},
            )
        )

    # --- Referrer-Policy ---
    if not h.get("referrer-policy"):
        findings.append(
            _finding(
                url,
                "missing-referrer-policy",
                "minor",
                "Missing Referrer-Policy header",
                "No Referrer-Policy header was present, so the browser's "
                "default (often permissive) referrer behavior applies.",
                {"header": "referrer-policy", "present": False},
            )
        )

    # --- Permissions-Policy ---
    if not h.get("permissions-policy"):
        findings.append(
            _finding(
                url,
                "missing-permissions-policy",
                "info",
                "Missing Permissions-Policy header",
                "No Permissions-Policy header was present to restrict "
                "access to powerful browser features.",
                {"header": "permissions-policy", "present": False},
            )
        )

    # --- Cookies ---
    for cookie_str in _iter_cookies(h.get("set-cookie")):
        name, missing = _check_cookie(cookie_str)
        if missing:
            findings.append(
                _finding(
                    url,
                    "insecure-cookie",
                    "moderate",
                    "Cookie missing security attribute(s)",
                    f"Cookie '{name}' is missing: {', '.join(missing)}.",
                    {"cookie": name, "missing": missing},
                )
            )

    # --- Server / X-Powered-By version disclosure ---
    for header_name in ("server", "x-powered-by"):
        value = h.get(header_name)
        if value and re.search(r"\d", value):
            findings.append(
                _finding(
                    url,
                    "server-version-disclosure",
                    "minor",
                    "Server header discloses version information",
                    f"The {header_name} header ('{value}') discloses "
                    "software/version details useful for targeting known "
                    "vulnerabilities.",
                    {"header": header_name, "value": value},
                )
            )

    return findings


# ---------------------------------------------------------------------------
# check_tls
# ---------------------------------------------------------------------------

_DAY = 86400


def _tls_protocol_version(protocol: str) -> Optional[float]:
    """Best-effort numeric TLS version from a security_details() protocol
    string (e.g. "TLS 1.2", "TLSv1.3", "SSL 3.0")."""
    p = protocol.strip()
    if "ssl" in p.lower():
        # Any SSL* protocol predates TLS 1.0 and is always weak.
        return 0.0
    m = re.search(r"(\d+(?:\.\d+)?)", p)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def check_tls(url: str, security_meta: Optional[Dict[str, Any]]) -> List[Finding]:
    """Evaluate transport security from crawler-collected TLS metadata.

    ``security_meta`` mirrors Playwright's ``response.security_details()``:
    ``{"protocol", "validFrom", "validTo" (unix seconds), "issuer",
    "subjectName"}``, or ``None`` if unavailable.
    """
    findings: List[Finding] = []

    if not _is_https(url):
        findings.append(
            _finding(
                url,
                "no-https",
                "critical",
                "Page served over plain HTTP",
                "The page was loaded over unencrypted HTTP; all traffic, "
                "including any credentials, can be intercepted or "
                "tampered with in transit.",
                {"url": url},
            )
        )
        return findings

    if not security_meta:
        # HTTPS but we have no certificate details — unknown, not a finding.
        return findings

    now = time.time()

    valid_to = security_meta.get("validTo")
    if valid_to is not None:
        remaining = valid_to - now
        if remaining < 14 * _DAY:
            findings.append(
                _finding(
                    url,
                    "cert-expiring",
                    "serious",
                    "TLS certificate is expiring soon",
                    "The TLS certificate expires in less than 14 days.",
                    {
                        "validTo": valid_to,
                        "days_remaining": round(remaining / _DAY, 2),
                    },
                )
            )
        elif remaining < 30 * _DAY:
            findings.append(
                _finding(
                    url,
                    "cert-expiring",
                    "moderate",
                    "TLS certificate is expiring soon",
                    "The TLS certificate expires in less than 30 days.",
                    {
                        "validTo": valid_to,
                        "days_remaining": round(remaining / _DAY, 2),
                    },
                )
            )

    protocol = security_meta.get("protocol")
    if protocol:
        version = _tls_protocol_version(protocol)
        if version is not None and version < 1.2:
            findings.append(
                _finding(
                    url,
                    "weak-tls",
                    "serious",
                    "Weak/outdated TLS protocol version negotiated",
                    f"The connection negotiated '{protocol}', which is "
                    "older than the minimum recommended TLS 1.2.",
                    {"protocol": protocol},
                )
            )

    return findings


# ---------------------------------------------------------------------------
# scan_secrets
# ---------------------------------------------------------------------------

# (rule, compiled pattern, severity, ambiguous)
_SECRET_PATTERNS: List[Tuple[str, "re.Pattern[str]", str, bool]] = [
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}"), "critical", False),
    (
        "google-api-key",
        re.compile(r"AIza[0-9A-Za-z\-_]{35}"),
        "critical",
        False,
    ),
    (
        "slack-token",
        re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}"),
        "critical",
        False,
    ),
    (
        "stripe-live-key",
        re.compile(r"sk_live_[0-9a-zA-Z]{16,}"),
        "critical",
        False,
    ),
    (
        "github-token",
        re.compile(r"gh[pousr]_[0-9A-Za-z]{36,}"),
        "critical",
        False,
    ),
    (
        "jwt",
        re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),
        "moderate",
        True,
    ),
    (
        "private-key-block",
        re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        "critical",
        False,
    ),
    (
        "generic-credential",
        re.compile(
            r"(?i)(api[_-]?key|secret|password|token)[\"']?\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']"
        ),
        "moderate",
        True,
    ),
]

_SECRET_TITLES = {
    "aws-access-key": "Possible AWS access key exposed",
    "google-api-key": "Possible Google API key exposed",
    "slack-token": "Possible Slack token exposed",
    "stripe-live-key": "Possible Stripe live secret key exposed",
    "github-token": "Possible GitHub token exposed",
    "jwt": "Possible JWT exposed in page content",
    "private-key-block": "Private key material exposed",
    "generic-credential": "Possible hard-coded credential exposed",
}

_SECRET_DETAIL_TEMPLATES = {
    "aws-access-key": "A string matching the AWS access key ID format "
    "(AKIA...) was found in {source}.",
    "google-api-key": "A string matching the Google API key format "
    "(AIza...) was found in {source}.",
    "slack-token": "A string matching the Slack token format (xox...) "
    "was found in {source}.",
    "stripe-live-key": "A string matching the Stripe *live* secret key "
    "format (sk_live_...) was found in {source}.",
    "github-token": "A string matching the GitHub token format "
    "(gh*_...) was found in {source}.",
    "jwt": "A JSON Web Token was found in {source}. JWTs are often "
    "benign (e.g. public session identifiers) but should be reviewed "
    "for sensitive claims.",
    "private-key-block": "A PEM private key block header was found in "
    "{source}.",
    "generic-credential": "A key/value pair matching common secret "
    "naming conventions (api_key, secret, password, token) was found in "
    "{source}; verify this is not a live credential.",
}


def _redact(match: str) -> str:
    """Redact a matched secret string, keeping only the first/last 4
    characters. Never returns enough of the original to reconstruct it
    for short matches."""
    if len(match) <= 8:
        return "…"
    return f"{match[:4]}…{match[-4:]}"


def scan_secrets(url: str, texts: Dict[str, str]) -> List[Finding]:
    """Scan page/script text for hard-coded secrets.

    ``texts`` maps a source name (e.g. "inline-html", "app.js") to its
    content. Identical (rule, redacted-match) pairs are deduplicated
    across all sources — only the first occurrence is kept. The full
    secret value is never included in a finding.
    """
    findings: List[Finding] = []
    seen: Set[Tuple[str, str]] = set()

    for source, content in (texts or {}).items():
        if not content:
            continue
        for rule, pattern, severity, ambiguous in _SECRET_PATTERNS:
            for m in pattern.finditer(content):
                match_str = m.group(0)
                redacted = _redact(match_str)
                key = (rule, redacted)
                if key in seen:
                    continue
                seen.add(key)
                findings.append(
                    _finding(
                        url,
                        rule,
                        severity,
                        _SECRET_TITLES[rule],
                        _SECRET_DETAIL_TEMPLATES[rule].format(source=source),
                        {
                            "source": source,
                            "rule": rule,
                            "match": redacted,
                            "offset": m.start(),
                        },
                        ambiguous=ambiguous,
                    )
                )

    return findings


# ---------------------------------------------------------------------------
# run_security
# ---------------------------------------------------------------------------

def run_security(
    url: str,
    headers: Dict[str, str],
    security_meta: Optional[Dict[str, Any]],
    html: str,
    scripts: Dict[str, str],
) -> List[Finding]:
    """Convenience entry point: run all Tier-0 security checks for a page.

    ``html`` is scanned for secrets as source "inline-html"; ``scripts``
    maps additional source names (e.g. script file names/urls) to their
    text content.
    """
    findings: List[Finding] = []
    findings.extend(check_headers(url, headers))
    findings.extend(check_tls(url, security_meta))

    texts: Dict[str, str] = {"inline-html": html or ""}
    if scripts:
        texts.update(scripts)
    findings.extend(scan_secrets(url, texts))

    return findings
