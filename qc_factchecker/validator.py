"""
QC / Fact-Checker — runs on GPT-5.5 via the OpenAI API.

Why a different model family:
  Different lab = genuinely independent failure modes. Claude (Researcher) and
  GPT (QC) are trained on different data mixes with different alignment choices.
  Two models from the same lab agreeing is not independence — it's correlated
  blind spots wearing two name tags. We want the QC to have a real shot at
  catching what the Researcher missed, not just rubber-stamp it.

Why GPT-5.5 specifically:
  OpenAI's Structured Outputs feature (response_format={"type":"json_object"})
  guarantees 100% JSON-schema compliance at the API level — no post-processing
  to guard against free-text verdicts. Fast and literal, which is exactly what
  an adversarial checker needs to be.

The system prompt instructs the model to try to FALSIFY the thesis — look for
disconfirming evidence, not confirming narrative. Even when the final verdict
is AGREE, the model MUST report what disconfirming evidence it considered and
why it wasn't decisive. That log is valuable for the weekly Auditor.
"""

import json
import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)

QC_MODEL = os.getenv("QC_MODEL", "gpt-5.5")

# gpt-5.5 is a REASONING model: its internal reasoning tokens are drawn from
# the SAME max_completion_tokens budget as the visible answer. If the budget is
# too small the model spends it all reasoning and returns EMPTY content, which
# then fails json.loads("") -> "Expecting value: line 1 column 1" and every
# trade gets blocked as a false QC_ERROR. 450 was too tight for the real QC
# prompt (the Researcher's Claude call learned the same lesson — see
# signal_generator: "500 was too low"). Give reasoning + JSON ample room.
QC_MAX_TOKENS = int(os.getenv("QC_MAX_COMPLETION_TOKENS", "3000"))

# Sentinel so a missing verdict key is distinguishable from a real one.
_MISSING = object()


@dataclass
class QCVerdict:
    verdict: str                            # "AGREE" | "DISAGREE" | "NEEDS_MORE_DATA"
    rationale: str                          # why this verdict
    disconfirming_evidence_considered: str  # logged for Auditor even on AGREE

    # True when QC never produced a usable answer — API error, timeout, bad
    # JSON, missing key, or a verdict string the model invented. False when
    # the model genuinely reached a conclusion, INCLUDING a genuine
    # NEEDS_MORE_DATA.
    #
    # Both cases block the trade, and that is correct. But they mean opposite
    # things operationally: a genuine NEEDS_MORE_DATA is the QC working, while
    # an errored one is the QC being unreachable. Collapsing them is what let
    # an exhausted OpenAI quota silently block every signal while the daily
    # summary reported a quiet market.
    errored: bool = False


SYSTEM_PROMPT = """You are an independent, skeptical adversarial reviewer for an AI equity
trading system. A Researcher AI has generated a trade signal. Your ONLY job is to try
to find reasons this trade is WRONG.

Actively investigate:
1. Technical claims that don't match the numbers: e.g., "breakout confirmed" when
   volume_ratio is 1.1× (the strategy requires ≥ 1.5×), or RSI claims that don't
   add up.
2. Fundamental claims made without a verifiable source: sentiment asserted without
   a real article, earnings beats stated without citing the filing date, contract
   wins the Researcher may have hallucinated.
3. Regime mismatch: does the market regime stated actually support this trade? Or is
   this a momentum pick in a bear regime dressed in bullish language?
4. Recency/narrative bias: is the rationale post-hoc storytelling around a stock
   that's already run? Would this thesis have existed 10 days ago?
5. Sector/macro risk: any India-specific or sector headwinds the Researcher ignored?

YOU MUST report specific disconfirming evidence you actively considered, even when
your final verdict is AGREE. "I checked X and it didn't change my verdict because Y"
is the required format. Vague affirmations are not acceptable.

Safe defaults:
- NEEDS_MORE_DATA if a key claim is unverifiable and would be material to the decision.
- DISAGREE if a technical criterion is clearly not met.
- AGREE only when the thesis is internally consistent and the key claims survive scrutiny.

Output ONLY valid JSON, no markdown:
{
  "verdict": "AGREE" or "DISAGREE" or "NEEDS_MORE_DATA",
  "rationale": "2-3 sentences explaining your verdict",
  "disconfirming_evidence_considered": "specific evidence/reasoning you checked that could have falsified this trade, and why it did or didn't change your verdict"
}"""


def validate_signal(signal) -> QCVerdict:
    """
    Calls GPT-5.5 to adversarially review the Researcher's trade signal.
    Safe default on any failure: NEEDS_MORE_DATA (blocks the trade).

    The returned verdict carries `errored`, which separates "QC considered
    this and is not satisfied" from "QC could not be reached". Both block,
    but only the second one means the system is degraded.

    Args:
        signal: TradeSignal dataclass from researcher/signal_generator.py
    """
    from openai import OpenAI

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        log.error("OPENAI_API_KEY not set — QC cannot run")
        return QCVerdict(
            verdict="NEEDS_MORE_DATA",
            rationale="OPENAI_API_KEY missing — cannot run QC. Blocking trade.",
            disconfirming_evidence_considered="N/A — API key not configured",
            errored=True,
        )

    client = OpenAI(api_key=api_key)

    prompt = f"""TRADE SIGNAL TO REVIEW:
  Ticker:            {signal.ticker}
  Sector:            {getattr(signal, "sector", "Unknown")}
  Regime:            {signal.regime.value}
  Strategy:          {signal.strategy_bucket}
  Direction:         {signal.direction}
  Technical score:   {signal.technical_score:.0f}/100
  Fundamental score: {signal.fundamental_score:.0f}/100
  Confidence:        {signal.confidence_score:.0f}%

RESEARCHER'S RATIONALE:
{signal.rationale}

Find reasons this is wrong. Output your verdict as JSON."""

    try:
        resp = client.chat.completions.create(
            model=QC_MODEL,
            # gpt-5.5 rejects `max_tokens` (400) and requires
            # `max_completion_tokens`, which must cover the reasoning tokens
            # AND the JSON answer — hence the generous QC_MAX_TOKENS.
            max_completion_tokens=QC_MAX_TOKENS,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},   # Structured Outputs
        )

        choice = resp.choices[0]
        raw = (choice.message.content or "").strip()

        # Empty content = the model produced no answer. On a reasoning model the
        # usual cause is the token budget being exhausted by reasoning
        # (finish_reason == "length"). Surface that precisely instead of letting
        # it fall through as a generic JSON parse error — a blocked trade
        # deserves an actionable reason, and this was a real production bug.
        if not raw:
            finish = getattr(choice, "finish_reason", "unknown")
            used = getattr(getattr(resp, "usage", None), "completion_tokens", "?")
            hint = (
                f"empty QC response (finish_reason={finish}, "
                f"completion_tokens={used}/{QC_MAX_TOKENS})"
            )
            if finish == "length":
                hint += " — reasoning exhausted the budget; raise QC_MAX_COMPLETION_TOKENS"
            log.error(f"QC returned no content: {hint}")
            return QCVerdict(
                verdict="NEEDS_MORE_DATA",
                rationale=f"QC produced no answer: {hint}. Blocking trade as safe default.",
                disconfirming_evidence_considered="N/A — empty response",
                errored=True,
            )

        data = json.loads(raw)

        # Read with a sentinel rather than defaulting straight to a valid
        # value: a response with no verdict key is a broken response, and
        # defaulting it to NEEDS_MORE_DATA would disguise that as a considered
        # verdict.
        verdict = data.get("verdict", _MISSING)
        # A verdict outside the allowed set means the model responded but did
        # not answer the question. Treated as errored: it is a broken QC, not
        # a considered "I need more data".
        malformed = verdict not in ("AGREE", "DISAGREE", "NEEDS_MORE_DATA")
        if malformed:
            log.warning(
                f"QC returned unexpected verdict {verdict!r} — treating as an "
                f"error, not a verdict, and blocking the trade."
            )
            verdict = "NEEDS_MORE_DATA"

        log.info(
            f"QC verdict for {signal.ticker}: {verdict} — "
            f"{data.get('rationale', '')[:80]}"
        )

        return QCVerdict(
            verdict=verdict,
            rationale=data.get("rationale", "No rationale provided."),
            disconfirming_evidence_considered=data.get(
                "disconfirming_evidence_considered", "None provided."
            ),
            errored=malformed,
        )

    except json.JSONDecodeError as e:
        log.error(f"QC returned malformed JSON: {e}")
        return QCVerdict(
            verdict="NEEDS_MORE_DATA",
            rationale=f"QC JSON parse error: {e} — blocking trade as safe default.",
            disconfirming_evidence_considered="N/A — parse error",
            errored=True,
        )
    except Exception as e:
        log.error(f"QC API call failed: {e}")
        return QCVerdict(
            verdict="NEEDS_MORE_DATA",
            rationale=f"QC API error ({type(e).__name__}: {e}) — blocking trade.",
            disconfirming_evidence_considered="N/A — API error",
            errored=True,
        )
