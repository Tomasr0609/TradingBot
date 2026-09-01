"""Clasificación tono positivo/negativo/neutro + relevancia."""

import re
from dataclasses import dataclass
from typing import Literal

Tone = Literal["positive", "negative", "neutral"]

# Léxicos simples (sin depender de modelo ML externo). Fail-safe.
POSITIVE_WORDS = {
    "surge", "rally", "bullish", "gain", "up", "rise", "soar", "breakout", "optimistic",
    "approve", "approval", "etf approved", "adoption", "growth", "record high", "all-time high",
    "buy", "accumulate", "support", "recovery", "positive", "upgrade", "launch",
}
NEGATIVE_WORDS = {
    "crash", "plunge", "bearish", "dump", "down", "fall", "drop", "collapse", "fear",
    "hack", "hacked", "exploit", "ban", "banned", "sec", "lawsuit", "sue", "penalty",
    "sell", "selloff", "liquidation", "liquidated", "rejection", "negative", "downgrade",
    "scam", "fraud", "crisis", "warning", "risk", "loss", "losses",
}
# Palabras que indican alta relevancia para el activo específico
HIGH_RELEVANCE_HINTS = {"btc", "bitcoin", "eth", "ethereum", "binance", "sec", "etf", "fed", "fomc", "rate"}


@dataclass
class Classification:
    tone: Tone
    tone_score: float  # -1 .. 1
    relevance: float  # 0 .. 1
    reason: str


CURRENCY_ALIASES = {
    "btc": ["btc", "bitcoin"],
    "eth": ["eth", "ethereum"],
    "sol": ["sol", "solana"],
    "bnb": ["bnb", "binance"],
}

def classify(title: str, symbol: str = "BTC/USDT") -> Classification:
    """Clasifica titular. Pura función, sin efectos colaterales."""
    text = title.lower()
    currency = symbol.split("/")[0].lower()
    aliases = CURRENCY_ALIASES.get(currency, [currency])

    # Conteo léxico
    pos_hits = sum(1 for w in POSITIVE_WORDS if w in text)
    neg_hits = sum(1 for w in NEGATIVE_WORDS if w in text)

    # Score -1..1 con normalización suave
    raw = pos_hits - neg_hits
    # Cada hit vale ~0.35, saturado en 1
    tone_score = max(-1.0, min(1.0, raw * 0.35))

    if tone_score > 0.25:
        tone: Tone = "positive"
    elif tone_score < -0.25:
        tone = "negative"
    else:
        tone = "neutral"

    # Relevancia: 0.5 base, +0.25 si menciona el currency (aliases), +0.15 si menciona hints, hasta 1.0
    relevance = 0.5
    if any(a in text for a in aliases):
        relevance += 0.25
    if any(h in text for h in HIGH_RELEVANCE_HINTS):
        relevance += 0.15
    # Penaliza títulos muy cortos o genéricos
    if len(text.split()) < 4:
        relevance = min(relevance, 0.4)
    relevance = max(0.0, min(1.0, relevance))

    reason = f"pos={pos_hits} neg={neg_hits} tone_score={tone_score:.2f} relevance={relevance:.2f} text='{title[:80]}'"

    return Classification(tone=tone, tone_score=tone_score, relevance=relevance, reason=reason)
