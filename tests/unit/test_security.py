"""Unit tests for tier0.security -- deterministic Tier-0 security checks.

No network, no browser: everything here operates on plain dicts/strings
that stand in for crawler-captured data.
"""

import json
import time

import pytest

from tier0 import security


HTTPS_URL = "https://example.com/"
HTTP_URL = "http://example.com/"


def rule_set(findings):
    return {f["rule"] for f in findings}


def find_by_rule(findings, rule):
    return [f for f in findings if f["rule"] == rule]


def assert_canonical_shape(finding):
    assert finding["pipeline"] == "security"
    assert finding["tier"] == 0
    assert set(
        ["url", "pipeline", "tier", "rule", "severity", "title", "detail", "evidence"]
    ) <= set(finding.keys())
    assert finding["severity"] in {"info", "minor", "moderate", "serious", "critical"}
    assert isinstance(finding["evidence"], dict)


# A baseline of headers that satisfies every check_headers rule, used as a
# starting point that individual tests strip one header from at a time.
GOOD_HEADERS = {
    "content-security-policy": "default-src 'self'; frame-ancestors 'self'",
    "strict-transport-security": "max-age=31536000; includeSubDomains",
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "permissions-policy": "geolocation=()",
}


# ---------------------------------------------------------------------------
# check_headers: baseline sanity
# ---------------------------------------------------------------------------

def test_good_headers_produce_no_findings():
    findings = security.check_headers(HTTPS_URL, dict(GOOD_HEADERS))
    assert findings == []


def test_all_findings_have_canonical_shape():
    findings = security.check_headers(HTTPS_URL, {})
    assert findings
    for f in findings:
        assert_canonical_shape(f)
        assert f["url"] == HTTPS_URL


# ---------------------------------------------------------------------------
# CSP
# ---------------------------------------------------------------------------

def test_missing_csp_fires():
    headers = dict(GOOD_HEADERS)
    del headers["content-security-policy"]
    findings = security.check_headers(HTTPS_URL, headers)
    matches = find_by_rule(findings, "missing-csp")
    assert len(matches) == 1
    assert matches[0]["severity"] == "serious"
    assert "ambiguous" not in matches[0]


def test_csp_present_does_not_fire_missing_csp():
    findings = security.check_headers(HTTPS_URL, dict(GOOD_HEADERS))
    assert "missing-csp" not in rule_set(findings)


@pytest.mark.parametrize(
    "csp_value",
    [
        "default-src 'self'; script-src 'self' 'unsafe-inline'",
        "default-src 'self'; script-src 'self' 'unsafe-eval'",
        "default-src 'self' 'unsafe-inline'",
    ],
)
def test_weak_csp_fires_for_unsafe_inline_or_eval(csp_value):
    headers = dict(GOOD_HEADERS)
    headers["content-security-policy"] = csp_value
    findings = security.check_headers(HTTPS_URL, headers)
    matches = find_by_rule(findings, "weak-csp")
    assert len(matches) == 1
    assert matches[0]["severity"] == "moderate"
    assert matches[0]["ambiguous"] is True


def test_weak_csp_does_not_fire_for_unsafe_inline_in_style_src_only():
    headers = dict(GOOD_HEADERS)
    headers["content-security-policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'"
    )
    findings = security.check_headers(HTTPS_URL, headers)
    assert "weak-csp" not in rule_set(findings)


def test_strict_csp_does_not_fire_weak_csp():
    findings = security.check_headers(HTTPS_URL, dict(GOOD_HEADERS))
    assert "weak-csp" not in rule_set(findings)


# ---------------------------------------------------------------------------
# HSTS
# ---------------------------------------------------------------------------

def test_missing_hsts_fires_on_https():
    headers = dict(GOOD_HEADERS)
    del headers["strict-transport-security"]
    findings = security.check_headers(HTTPS_URL, headers)
    matches = find_by_rule(findings, "missing-hsts")
    assert len(matches) == 1
    assert matches[0]["severity"] == "serious"


def test_missing_hsts_does_not_fire_on_http():
    headers = dict(GOOD_HEADERS)
    del headers["strict-transport-security"]
    findings = security.check_headers(HTTP_URL, headers)
    assert "missing-hsts" not in rule_set(findings)


def test_short_hsts_max_age_fires():
    headers = dict(GOOD_HEADERS)
    headers["strict-transport-security"] = "max-age=3600"
    findings = security.check_headers(HTTPS_URL, headers)
    matches = find_by_rule(findings, "short-hsts")
    assert len(matches) == 1
    assert matches[0]["severity"] == "moderate"
    assert matches[0]["evidence"]["max_age"] == 3600


def test_long_hsts_max_age_does_not_fire_short_hsts():
    headers = dict(GOOD_HEADERS)
    headers["strict-transport-security"] = "max-age=15552000"
    findings = security.check_headers(HTTPS_URL, headers)
    assert "short-hsts" not in rule_set(findings)


# ---------------------------------------------------------------------------
# Clickjacking / frame protection
# ---------------------------------------------------------------------------

def test_missing_frame_protection_fires_without_xfo_or_frame_ancestors():
    headers = dict(GOOD_HEADERS)
    del headers["x-frame-options"]
    headers["content-security-policy"] = "default-src 'self'"  # no frame-ancestors
    findings = security.check_headers(HTTPS_URL, headers)
    matches = find_by_rule(findings, "missing-frame-protection")
    assert len(matches) == 1
    assert matches[0]["severity"] == "moderate"


def test_frame_protection_satisfied_by_xfo_alone():
    headers = dict(GOOD_HEADERS)
    headers["content-security-policy"] = "default-src 'self'"  # no frame-ancestors
    findings = security.check_headers(HTTPS_URL, headers)
    assert "missing-frame-protection" not in rule_set(findings)


def test_frame_protection_satisfied_by_csp_frame_ancestors_alone():
    headers = dict(GOOD_HEADERS)
    del headers["x-frame-options"]
    # GOOD_HEADERS CSP already includes frame-ancestors 'self'
    findings = security.check_headers(HTTPS_URL, headers)
    assert "missing-frame-protection" not in rule_set(findings)


# ---------------------------------------------------------------------------
# X-Content-Type-Options
# ---------------------------------------------------------------------------

def test_missing_xcto_fires_when_absent():
    headers = dict(GOOD_HEADERS)
    del headers["x-content-type-options"]
    findings = security.check_headers(HTTPS_URL, headers)
    matches = find_by_rule(findings, "missing-xcto")
    assert len(matches) == 1
    assert matches[0]["severity"] == "minor"


def test_missing_xcto_fires_when_value_is_wrong():
    headers = dict(GOOD_HEADERS)
    headers["x-content-type-options"] = "sniff"
    findings = security.check_headers(HTTPS_URL, headers)
    assert "missing-xcto" in rule_set(findings)


def test_xcto_nosniff_case_insensitive_does_not_fire():
    headers = dict(GOOD_HEADERS)
    headers["x-content-type-options"] = "NoSniff"
    findings = security.check_headers(HTTPS_URL, headers)
    assert "missing-xcto" not in rule_set(findings)


# ---------------------------------------------------------------------------
# Referrer-Policy / Permissions-Policy
# ---------------------------------------------------------------------------

def test_missing_referrer_policy_fires():
    headers = dict(GOOD_HEADERS)
    del headers["referrer-policy"]
    findings = security.check_headers(HTTPS_URL, headers)
    matches = find_by_rule(findings, "missing-referrer-policy")
    assert len(matches) == 1
    assert matches[0]["severity"] == "minor"


def test_missing_permissions_policy_fires():
    headers = dict(GOOD_HEADERS)
    del headers["permissions-policy"]
    findings = security.check_headers(HTTPS_URL, headers)
    matches = find_by_rule(findings, "missing-permissions-policy")
    assert len(matches) == 1
    assert matches[0]["severity"] == "info"


# ---------------------------------------------------------------------------
# Cookies
# ---------------------------------------------------------------------------

def test_cookie_missing_all_flags_fires_with_all_missing_listed():
    headers = dict(GOOD_HEADERS)
    headers["set-cookie"] = "session=SECRETVALUE123; Path=/"
    findings = security.check_headers(HTTPS_URL, headers)
    matches = find_by_rule(findings, "insecure-cookie")
    assert len(matches) == 1
    evidence = matches[0]["evidence"]
    assert evidence["cookie"] == "session"
    assert set(evidence["missing"]) == {"Secure", "HttpOnly", "SameSite"}
    # cookie value must never leak into the finding
    assert "SECRETVALUE123" not in json.dumps(matches[0])


def test_cookie_fully_flagged_does_not_fire():
    headers = dict(GOOD_HEADERS)
    headers["set-cookie"] = "session=abc; Path=/; Secure; HttpOnly; SameSite=Strict"
    findings = security.check_headers(HTTPS_URL, headers)
    assert "insecure-cookie" not in rule_set(findings)


@pytest.mark.parametrize(
    "cookie_str,missing_expected",
    [
        ("session=abc; Path=/; HttpOnly; SameSite=Strict", {"Secure"}),
        ("session=abc; Path=/; Secure; SameSite=Strict", {"HttpOnly"}),
        ("session=abc; Path=/; Secure; HttpOnly", {"SameSite"}),
    ],
)
def test_cookie_matrix_single_missing_flag(cookie_str, missing_expected):
    headers = dict(GOOD_HEADERS)
    headers["set-cookie"] = cookie_str
    findings = security.check_headers(HTTPS_URL, headers)
    matches = find_by_rule(findings, "insecure-cookie")
    assert len(matches) == 1
    assert set(matches[0]["evidence"]["missing"]) == missing_expected


def test_multiple_cookies_produce_multiple_findings_with_correct_names():
    headers = dict(GOOD_HEADERS)
    headers["set-cookie"] = [
        "a=1; Path=/",
        "b=2; Path=/; Secure; HttpOnly; SameSite=Lax",
    ]
    findings = security.check_headers(HTTPS_URL, headers)
    matches = find_by_rule(findings, "insecure-cookie")
    assert len(matches) == 1
    assert matches[0]["evidence"]["cookie"] == "a"


# ---------------------------------------------------------------------------
# Server / X-Powered-By version disclosure
# ---------------------------------------------------------------------------

def test_server_version_disclosure_fires():
    headers = dict(GOOD_HEADERS)
    headers["server"] = "Apache/2.4.41 (Ubuntu)"
    findings = security.check_headers(HTTPS_URL, headers)
    matches = find_by_rule(findings, "server-version-disclosure")
    assert len(matches) == 1
    assert matches[0]["evidence"]["header"] == "server"


def test_x_powered_by_version_disclosure_fires():
    headers = dict(GOOD_HEADERS)
    headers["x-powered-by"] = "PHP/7.4.3"
    findings = security.check_headers(HTTPS_URL, headers)
    matches = find_by_rule(findings, "server-version-disclosure")
    assert len(matches) == 1
    assert matches[0]["evidence"]["header"] == "x-powered-by"


def test_server_header_without_version_does_not_fire():
    headers = dict(GOOD_HEADERS)
    headers["server"] = "cloudflare"
    findings = security.check_headers(HTTPS_URL, headers)
    assert "server-version-disclosure" not in rule_set(findings)


def test_headers_are_case_insensitive_keys():
    headers = {k.upper(): v for k, v in GOOD_HEADERS.items()}
    findings = security.check_headers(HTTPS_URL, headers)
    assert findings == []


# ---------------------------------------------------------------------------
# check_tls
# ---------------------------------------------------------------------------

def test_http_url_fires_no_https_critical():
    findings = security.check_tls(HTTP_URL, None)
    assert len(findings) == 1
    assert findings[0]["rule"] == "no-https"
    assert findings[0]["severity"] == "critical"


def test_http_url_ignores_security_meta():
    # Even if security_meta is somehow present, http:// is still critical
    # and no TLS-detail checks apply.
    findings = security.check_tls(HTTP_URL, {"protocol": "TLS 1.3", "validTo": time.time() + 1e9})
    rules = rule_set(findings)
    assert rules == {"no-https"}


def test_https_with_no_security_meta_produces_no_findings():
    findings = security.check_tls(HTTPS_URL, None)
    assert findings == []


def test_cert_expiring_within_14_days_is_serious():
    valid_to = time.time() + 10 * 86400
    findings = security.check_tls(HTTPS_URL, {"protocol": "TLS 1.3", "validTo": valid_to})
    matches = find_by_rule(findings, "cert-expiring")
    assert len(matches) == 1
    assert matches[0]["severity"] == "serious"


def test_cert_expiring_within_30_days_is_moderate():
    valid_to = time.time() + 20 * 86400
    findings = security.check_tls(HTTPS_URL, {"protocol": "TLS 1.3", "validTo": valid_to})
    matches = find_by_rule(findings, "cert-expiring")
    assert len(matches) == 1
    assert matches[0]["severity"] == "moderate"


def test_cert_far_from_expiry_produces_no_finding():
    valid_to = time.time() + 90 * 86400
    findings = security.check_tls(HTTPS_URL, {"protocol": "TLS 1.3", "validTo": valid_to})
    assert "cert-expiring" not in rule_set(findings)


@pytest.mark.parametrize("protocol", ["TLS 1.0", "TLSv1.0", "TLS 1.1", "SSL 3.0", "SSLv3"])
def test_weak_tls_protocol_fires(protocol):
    valid_to = time.time() + 90 * 86400
    findings = security.check_tls(HTTPS_URL, {"protocol": protocol, "validTo": valid_to})
    matches = find_by_rule(findings, "weak-tls")
    assert len(matches) == 1
    assert matches[0]["severity"] == "serious"


@pytest.mark.parametrize("protocol", ["TLS 1.2", "TLSv1.2", "TLS 1.3", "TLSv1.3"])
def test_modern_tls_protocol_does_not_fire(protocol):
    valid_to = time.time() + 90 * 86400
    findings = security.check_tls(HTTPS_URL, {"protocol": protocol, "validTo": valid_to})
    assert "weak-tls" not in rule_set(findings)


def test_check_tls_findings_have_canonical_shape():
    findings = security.check_tls(HTTP_URL, None)
    for f in findings:
        assert_canonical_shape(f)


# ---------------------------------------------------------------------------
# scan_secrets
# ---------------------------------------------------------------------------

SECRET_SAMPLES = {
    "aws-access-key": "AKIAIOSFODNN7EXAMPLE",
    "google-api-key": "AIza" + "x" * 35,
    "slack-token": "xoxb-" + "1234567890abcd",
    "stripe-live-key": "sk_live_" + "abcdefgh12345678",
    "github-token": "ghp_" + "A" * 36,
    "jwt": "eyJ" + "A" * 12 + "." + "B" * 15 + "." + "C" * 8,
    "private-key-block": "-----BEGIN RSA PRIVATE KEY-----",
    "generic-credential": 'api_key: "abcdefgh12345"',
}


@pytest.mark.parametrize("rule,secret", list(SECRET_SAMPLES.items()))
def test_each_secret_pattern_fires_and_is_redacted(rule, secret):
    content = f"const cfg = {{ value: '{secret}' }};"
    findings = security.scan_secrets(HTTPS_URL, {"inline-html": content})
    matches = find_by_rule(findings, rule)
    assert len(matches) == 1, f"expected exactly one {rule} finding, got {matches}"

    finding = matches[0]
    assert_canonical_shape(finding)

    dumped = json.dumps(finding)
    assert secret not in dumped, "full secret leaked into finding!"

    redacted = finding["evidence"]["match"]
    assert "…" in redacted
    assert secret not in redacted


def test_aws_key_redaction_keeps_only_first_and_last_four_chars():
    secret = SECRET_SAMPLES["aws-access-key"]
    findings = security.scan_secrets(HTTPS_URL, {"inline-html": secret})
    matches = find_by_rule(findings, "aws-access-key")
    assert matches[0]["evidence"]["match"] == f"{secret[:4]}…{secret[-4:]}"
    assert matches[0]["severity"] == "critical"
    assert "ambiguous" not in matches[0]


def test_offset_points_at_match_start():
    secret = SECRET_SAMPLES["aws-access-key"]
    content = f"prefix-padding----{secret}----suffix"
    findings = security.scan_secrets(HTTPS_URL, {"inline-html": content})
    matches = find_by_rule(findings, "aws-access-key")
    assert matches[0]["evidence"]["offset"] == content.index(secret)


def test_jwt_is_marked_ambiguous_and_moderate():
    secret = SECRET_SAMPLES["jwt"]
    findings = security.scan_secrets(HTTPS_URL, {"inline-html": secret})
    matches = find_by_rule(findings, "jwt")
    assert len(matches) == 1
    assert matches[0]["severity"] == "moderate"
    assert matches[0]["ambiguous"] is True


def test_generic_credential_is_marked_ambiguous_and_moderate():
    secret = SECRET_SAMPLES["generic-credential"]
    findings = security.scan_secrets(HTTPS_URL, {"inline-html": secret})
    matches = find_by_rule(findings, "generic-credential")
    assert len(matches) == 1
    assert matches[0]["severity"] == "moderate"
    assert matches[0]["ambiguous"] is True


def test_named_live_key_rules_are_critical_and_not_ambiguous():
    for rule in (
        "aws-access-key",
        "google-api-key",
        "slack-token",
        "stripe-live-key",
        "github-token",
        "private-key-block",
    ):
        secret = SECRET_SAMPLES[rule]
        findings = security.scan_secrets(HTTPS_URL, {"inline-html": secret})
        matches = find_by_rule(findings, rule)
        assert len(matches) == 1
        assert matches[0]["severity"] == "critical", rule
        assert "ambiguous" not in matches[0], rule


def test_dedup_identical_rule_and_redacted_match_across_sources():
    secret = SECRET_SAMPLES["aws-access-key"]
    findings = security.scan_secrets(
        HTTPS_URL,
        {"inline-html": secret, "app.js": secret},
    )
    matches = find_by_rule(findings, "aws-access-key")
    assert len(matches) == 1
    # first-seen source should win
    assert matches[0]["evidence"]["source"] == "inline-html"


def test_dedup_within_a_single_source_repeated_secret():
    secret = SECRET_SAMPLES["aws-access-key"]
    content = f"{secret} ... later in the same file ... {secret}"
    findings = security.scan_secrets(HTTPS_URL, {"inline-html": content})
    matches = find_by_rule(findings, "aws-access-key")
    assert len(matches) == 1


def test_different_secrets_of_same_rule_are_not_deduped():
    secret_a = "AKIAIOSFODNN7EXAMPLE"
    secret_b = "AKIABBBBBBBBBBBBBBBB"
    findings = security.scan_secrets(HTTPS_URL, {"inline-html": f"{secret_a} {secret_b}"})
    matches = find_by_rule(findings, "aws-access-key")
    assert len(matches) == 2


def test_no_secrets_in_clean_text_produces_no_findings():
    findings = security.scan_secrets(
        HTTPS_URL, {"inline-html": "<html><body>hello world</body></html>"}
    )
    assert findings == []


def test_scan_secrets_handles_empty_and_none_texts():
    assert security.scan_secrets(HTTPS_URL, {}) == []
    assert security.scan_secrets(HTTPS_URL, {"inline-html": ""}) == []


# ---------------------------------------------------------------------------
# run_security
# ---------------------------------------------------------------------------

def test_run_security_aggregates_all_three_checks():
    secret = SECRET_SAMPLES["aws-access-key"]
    findings = security.run_security(
        HTTPS_URL,
        headers={},  # triggers several header findings
        security_meta={"protocol": "TLS 1.0", "validTo": time.time() + 5 * 86400},
        html=f"<script>{secret}</script>",
        scripts={"app.js": SECRET_SAMPLES["jwt"]},
    )
    rules = rule_set(findings)
    assert "missing-csp" in rules  # from check_headers
    assert "cert-expiring" in rules  # from check_tls
    assert "weak-tls" in rules  # from check_tls
    assert "aws-access-key" in rules  # from scan_secrets (html)
    assert "jwt" in rules  # from scan_secrets (scripts)
    for f in findings:
        assert_canonical_shape(f)


def test_run_security_scans_html_as_inline_html_source():
    secret = SECRET_SAMPLES["aws-access-key"]
    findings = security.run_security(
        HTTPS_URL,
        headers=dict(GOOD_HEADERS),
        security_meta=None,
        html=secret,
        scripts={},
    )
    matches = find_by_rule(findings, "aws-access-key")
    assert len(matches) == 1
    assert matches[0]["evidence"]["source"] == "inline-html"


def test_run_security_with_no_scripts_or_html():
    findings = security.run_security(
        HTTPS_URL,
        headers=dict(GOOD_HEADERS),
        security_meta=None,
        html="",
        scripts={},
    )
    assert findings == []
