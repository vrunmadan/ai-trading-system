"""
.env.example must stay in sync with what the code actually reads.

It had drifted to documenting 24 of 47 variables, and documenting two
(GMAIL_APP_PASSWORD, UNIVERSE_CSV_PATH) that nothing read. Among the missing
were RESEND_API_KEY — without which the system runs full cycles and silently
sends nothing — and LONG_STOP_LOSS_PCT / TRAILING_STOP_PCT, the entire exit
design. This test fails the build rather than letting that happen again.
"""

import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", "tests"}

GETENV = re.compile(r"""os\.getenv\(\s*["']([A-Z_0-9]+)["']""")
DECLARED = re.compile(r"^([A-Z_0-9]+)=", re.M)


def _code_vars() -> set:
    found = set()
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if not f.endswith(".py"):
                continue
            path = os.path.join(root, f)
            try:
                src = open(path, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            found.update(GETENV.findall(src))
    return found


def _documented_vars() -> set:
    path = os.path.join(ROOT, ".env.example")
    return set(DECLARED.findall(open(path, encoding="utf-8").read()))


def test_every_variable_the_code_reads_is_documented():
    missing = sorted(_code_vars() - _documented_vars())
    assert not missing, (
        "These are read via os.getenv but absent from .env.example, so a "
        "fresh deploy silently uses code defaults: " + ", ".join(missing)
    )


def test_no_documented_variable_is_dead():
    extra = sorted(_documented_vars() - _code_vars())
    assert not extra, (
        "These are documented in .env.example but read by nothing. Stale "
        "config entries are worse than none — they imply a knob that does "
        "not exist: " + ", ".join(extra)
    )


def test_no_real_credentials_are_committed():
    """Every credential field must ship empty."""
    path = os.path.join(ROOT, ".env.example")
    secret_like = re.compile(
        r"^((?:ANTHROPIC|OPENAI|GOOGLE|KITE|RESEND|APPROVAL|SCREENER|SHEETS)"
        r"[A-Z_0-9]*(?:KEY|SECRET|TOKEN|PASSWORD|ID))=(.*)$",
        re.M,
    )
    populated = [
        (name, val) for name, val in secret_like.findall(
            open(path, encoding="utf-8").read()
        ) if val.strip()
    ]
    assert not populated, f"credential fields must be blank: {populated}"


@pytest.mark.parametrize("critical", [
    "RESEND_API_KEY",       # without it the system silently sends nothing
    "OPENAI_API_KEY",       # without quota, QC blocks every trade
    "ANTHROPIC_API_KEY",    # without it, no signal is generated
    "APPROVAL_SECRET",      # without it, every approve link fails to verify
    "LEDGER_DB_PATH",       # wrong value silently loses all history on deploy
    "LONG_STOP_LOSS_PCT",   # the exit design
    "TRAILING_STOP_PCT",
])
def test_load_bearing_variables_are_present(critical):
    """
    Spot-checks the ones whose absence causes a SILENT failure, since those
    are the entries most costly to leave undocumented.
    """
    assert critical in _documented_vars()
