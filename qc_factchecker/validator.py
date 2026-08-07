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


@dataclass
class QCVerdict:
    verdict: str                            # "AGREE" | "DISAGREE" | "NEEDS_MORE_DATA"
    rationale: str                          # why this verdict
    disconfirming_evidence_considered: str  # logged for Auditor even on AGREE


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
            max_tokens=450,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},   # Structured Outputs
        )
        raw = resp.choices[0].message.content.strip()
        data = json.loads(raw)

        verdict = data.get("verdict", "NEEDS_MORE_DATA")
        if verdict not in ("AGREE", "DISAGREE", "NEEDS_MORE_DATA"):
            log.warning(f"QC returned unexpected verdict '{verdict}' — defaulting to NEEDS_MORE_DATA")
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
        )

    except json.JSONDecodeError as e:
        log.error(f"QC returned malformed JSON: {e}")
        return QCVerdict(
            verdict="NEEDS_MORE_DATA",
            rationale=f"QC JSON parse error: {e} — blocking trade as safe default.",
            disconfirming_evidence_considered="N/A — parse error",
        )
    except Exception as e:
        log.error(f"QC API call failed: {e}")
        return QCVerdict(
            verdict="NEEDS_MORE_DATA",
            rationale=f"QC API error ({type(e).__name__}: {e}) — blocking trade.",
            disconfirming_evidence_considered="N/A — API error",
        )
