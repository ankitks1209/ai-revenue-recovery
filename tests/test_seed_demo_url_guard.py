"""Strict demo URL guard — only exact demo SQLite filenames accepted."""
from __future__ import annotations

import subprocess
import sys

import pytest

from scripts.seed_demo import DEMO_AUDIT_URL, DEMO_PAYMENTS_URL, _is_demo_url


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "scripts/seed_demo.py", *args], capture_output=True, text=True)


def test_exact_demo_urls_are_accepted():
    assert _is_demo_url(DEMO_PAYMENTS_URL, "payments") is True
    assert _is_demo_url(DEMO_AUDIT_URL, "audit") is True
    assert DEMO_PAYMENTS_URL == "sqlite:///demo_failed_payments.db"
    assert DEMO_AUDIT_URL == "sqlite:///demo_audit_log.db"
    # subprocess with explicit exact URLs succeeds
    r = _run("--seed", "42", "--count", "60", "--confirm",
             "--payments-db-url", DEMO_PAYMENTS_URL,
             "--audit-db-url", DEMO_AUDIT_URL)
    assert r.returncode == 0, r.stderr


def test_normal_db_urls_are_rejected():
    assert _is_demo_url("sqlite:///failed_payments.db", "payments") is False
    assert _is_demo_url("sqlite:///audit_log.db", "audit") is False
    r1 = _run("--seed", "42", "--count", "60", "--confirm",
              "--payments-db-url", "sqlite:///failed_payments.db")
    assert r1.returncode == 2
    r2 = _run("--seed", "42", "--count", "60", "--confirm",
              "--audit-db-url", "sqlite:///audit_log.db")
    assert r2.returncode == 2
    # cross-kind also rejected
    assert _is_demo_url(DEMO_AUDIT_URL, "payments") is False
    assert _is_demo_url(DEMO_PAYMENTS_URL, "audit") is False


def test_alternate_directory_urls_containing_demo_filename_are_rejected():
    # any path that merely contains the demo filename but is not the exact URL must be rejected
    bad_payments = [
        "sqlite:////tmp/demo_failed_payments.db",
        "sqlite:///subdir/demo_failed_payments.db",
        "sqlite:///tmp/demo_failed_payments.db",
        "sqlite:///./demo_failed_payments.db",
        "sqlite:///demo_failed_payments.db?mode=memory",
        "sqlite:///demo_failed_payments.db ",
        " sqlite:///demo_failed_payments.db",
    ]
    bad_audits = [
        "sqlite:////tmp/demo_audit_log.db",
        "sqlite:///subdir/demo_audit_log.db",
        "sqlite:///tmp/demo_audit_log.db",
        "sqlite:///./demo_audit_log.db",
        "sqlite:///demo_audit_log.db?mode=memory",
    ]
    for url in bad_payments:
        assert _is_demo_url(url, "payments") is False, url
        r = _run("--seed", "42", "--count", "60", "--confirm", "--payments-db-url", url)
        assert r.returncode == 2, f"should reject {url}: {r.stderr}"
    for url in bad_audits:
        assert _is_demo_url(url, "audit") is False, url
        r = _run("--seed", "42", "--count", "60", "--confirm", "--audit-db-url", url)
        assert r.returncode == 2, f"should reject {url}: {r.stderr}"
    # traversal
    assert _is_demo_url("sqlite:///../demo_failed_payments.db", "payments") is False
    r = _run("--seed", "42", "--count", "60", "--confirm",
             "--payments-db-url", "sqlite:///../demo_failed_payments.db")
    assert r.returncode == 2


def test_non_sqlite_urls_are_rejected():
    bad = [
        "postgresql://localhost/demo_failed_payments.db",
        "postgresql://localhost/demo_audit_log.db",
        "sqlite:///:memory:",
        "sqlite://",
        "demo_failed_payments.db",
        "demo_audit_log.db",
        "",
        "sqlite:///demo_failed_payments.db; DROP TABLE",
    ]
    for url in bad:
        assert _is_demo_url(url, "payments") is False, url
        assert _is_demo_url(url, "audit") is False, url
    r = _run("--seed", "42", "--count", "60", "--confirm",
             "--payments-db-url", "postgresql://localhost/demo_failed_payments.db")
    assert r.returncode == 2
    r = _run("--seed", "42", "--count", "60", "--confirm",
             "--audit-db-url", "postgresql://localhost/demo_audit_log.db")
    assert r.returncode == 2
    r = _run("--seed", "42", "--count", "60", "--confirm",
             "--payments-db-url", "sqlite:///:memory:")
    assert r.returncode == 2
