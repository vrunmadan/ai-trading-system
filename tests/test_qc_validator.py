"""
QCVerdict.errored — separating "QC decided" from "QC could not be reached".

Both outcomes block the trade, which is correct. But they mean opposite things
operationally, and collapsing them is what let an exhausted OpenAI quota block
every signal for an unknown stretch while the daily summary reported that no
candidate had cleared the confidence threshold.
"""

import json

import pytest


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _Message:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Message(content)


class _Response:
    def __init__(self, content):
        self.choices = [_Choice(content)]


class FakeCompletions:
    """Returns a canned body, or raises a canned exception."""

    def __init__(self, content=None, exc=None):
        self.content = content
        self.exc = exc
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        if self.exc:
            raise self.exc
        return _Response(self.content)


class FakeOpenAI:
    def __init__(self, content=None, exc=None):
        self._completions = FakeCompletions(content, exc)
        self.chat = type("chat", (), {"completions": self._completions})()

    def __call__(self, *a, **kw):        # stands in for the OpenAI(...) ctor
        return self


@pytest.fixture()
def signal():
    from researcher.regime_classifier import Regime
    from researcher.signal_generator import TradeSignal

    return TradeSignal(
        ticker="TITAN", sector="CONSUMER GOODS", exchange="NSE",
        regime=Regime.BULL, strategy_bucket="52wk_breakout", direction="BUY",
        technical_score=88.0, fundamental_score=79.0, confidence_score=84.6,
        rationale="Breakout on 1.9x volume, RSI 63, Supertrend green.",
    )


def _run(monkeypatch, signal, content=None, exc=None, key="sk-test"):
    import openai

    if key is None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    else:
        monkeypatch.setenv("OPENAI_API_KEY", key)

    fake = FakeOpenAI(content=content, exc=exc)
    monkeypatch.setattr(openai, "OpenAI", fake)

    from qc_factchecker.validator import validate_signal

    return validate_signal(signal), fake


def _body(verdict, rationale="because", evidence="checked X"):
    return json.dumps({
        "verdict": verdict,
        "rationale": rationale,
        "disconfirming_evidence_considered": evidence,
    })


# ---------------------------------------------------------------------------
# Genuine verdicts — the model answered, so errored stays False
# ---------------------------------------------------------------------------

def test_agree_is_not_errored(monkeypatch, signal):
    v, _ = _run(monkeypatch, signal, content=_body("AGREE"))
    assert v.verdict == "AGREE"
    assert v.errored is False


def test_genuine_disagree_is_not_errored(monkeypatch, signal):
    v, _ = _run(
        monkeypatch, signal,
        content=_body("DISAGREE", "Volume ratio is 1.1x, strategy needs 1.5x."),
    )
    assert v.verdict == "DISAGREE"
    assert v.errored is False
    assert "1.1x" in v.rationale


def test_genuine_needs_more_data_is_not_errored(monkeypatch, signal):
    """
    The important one. A considered NEEDS_MORE_DATA is QC working correctly,
    and must not be reported as a system fault.
    """
    v, _ = _run(
        monkeypatch, signal,
        content=_body("NEEDS_MORE_DATA", "Cannot verify the contract-win claim."),
    )
    assert v.verdict == "NEEDS_MORE_DATA"
    assert v.errored is False


def test_genuine_verdict_preserves_rationale_and_evidence(monkeypatch, signal):
    v, _ = _run(
        monkeypatch, signal,
        content=_body("AGREE", "Claims hold up.", "Checked the 52wk high."),
    )
    assert v.rationale == "Claims hold up."
    assert v.disconfirming_evidence_considered == "Checked the 52wk high."


# ---------------------------------------------------------------------------
# Failure paths — QC never produced a usable answer
# ---------------------------------------------------------------------------

def test_api_error_is_errored(monkeypatch, signal):
    """The live failure: 429 insufficient_quota."""
    class RateLimitError(Exception):
        pass

    v, _ = _run(
        monkeypatch, signal,
        exc=RateLimitError("Error code: 429 - insufficient_quota"),
    )
    assert v.verdict == "NEEDS_MORE_DATA"
    assert v.errored is True
    assert "429" in v.rationale


def test_timeout_is_errored(monkeypatch, signal):
    v, _ = _run(monkeypatch, signal, exc=TimeoutError("request timed out"))
    assert v.verdict == "NEEDS_MORE_DATA"
    assert v.errored is True


def test_malformed_json_is_errored(monkeypatch, signal):
    v, _ = _run(monkeypatch, signal, content="not json at all {{{")
    assert v.verdict == "NEEDS_MORE_DATA"
    assert v.errored is True
    assert "parse" in v.rationale.lower()


def test_missing_api_key_is_errored(monkeypatch, signal):
    v, _ = _run(monkeypatch, signal, content=_body("AGREE"), key=None)
    assert v.verdict == "NEEDS_MORE_DATA"
    assert v.errored is True
    assert "OPENAI_API_KEY" in v.rationale


def test_invented_verdict_string_is_errored(monkeypatch, signal):
    """
    The model responded but did not answer the question. That is a broken QC,
    not a considered "I need more data".
    """
    v, _ = _run(monkeypatch, signal, content=_body("MAYBE"))
    assert v.verdict == "NEEDS_MORE_DATA"
    assert v.errored is True


def test_missing_verdict_key_is_errored(monkeypatch, signal):
    v, _ = _run(monkeypatch, signal, content=json.dumps({"rationale": "hmm"}))
    assert v.verdict == "NEEDS_MORE_DATA"
    assert v.errored is True


# ---------------------------------------------------------------------------
# The distinction itself
# ---------------------------------------------------------------------------

def test_blocking_verdicts_are_distinguishable_by_errored_alone(
    monkeypatch, signal
):
    """
    Both block the trade and both carry verdict NEEDS_MORE_DATA. `errored` is
    the only thing separating "QC is not satisfied" from "QC is down", so the
    alerting layer depends on exactly this.
    """
    genuine, _ = _run(
        monkeypatch, signal,
        content=_body("NEEDS_MORE_DATA", "Unverifiable earnings claim."),
    )
    broken, _ = _run(monkeypatch, signal, exc=RuntimeError("429 quota"))

    assert genuine.verdict == broken.verdict == "NEEDS_MORE_DATA"
    assert genuine.errored is False
    assert broken.errored is True


def test_errored_defaults_to_false():
    """A QCVerdict built without the flag must not look like a failure."""
    from qc_factchecker.validator import QCVerdict

    v = QCVerdict(verdict="AGREE", rationale="r", disconfirming_evidence_considered="e")
    assert v.errored is False
