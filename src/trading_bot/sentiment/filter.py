"""Filtro de contexto por noticias/sentimiento - SOLO reduce/veta, nunca genera.

Contrato Fase 5:
- Input adicional al RiskEngine que puede reducir tamaño o vetar, nunca generar señal.
- Pausa configurable antes/después de eventos macro (FOMC).
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional, Literal

from trading_bot.sentiment.provider import Headline
from trading_bot.sentiment.classifier import classify, Classification
from trading_bot.risk_management.models import RiskDecision, RiskRule

logger = logging.getLogger(__name__)


@dataclass
class SentimentFilterResult:
    action: Literal["allow", "reduce", "veto"]
    reason: str
    tone: str
    tone_score: float
    relevance: float
    reduce_factor: float  # 0..1, ej 0.5 = reduce 50%
    headlines: list[Headline]


class SentimentFilter:
    """
    Filtro puro: decide allow/reduce/veto basado en headlines recientes.
    No tiene side effects ni genera señales.
    """

    def __init__(
        self,
        veto_threshold: float = -0.6,  # tone_score <= -0.6 y relevance >=0.8 -> veto
        reduce_threshold: float = -0.3,  # tone_score <= -0.3 y relevance >=0.6 -> reduce 50%
        relevance_veto: float = 0.8,
        relevance_reduce: float = 0.6,
        reduce_factor: float = 0.5,
    ):
        self.veto_threshold = veto_threshold
        self.reduce_threshold = reduce_threshold
        self.relevance_veto = relevance_veto
        self.relevance_reduce = relevance_reduce
        self.reduce_factor = reduce_factor

    def evaluate(self, headlines: list[Headline], symbol: str) -> SentimentFilterResult:
        if not headlines:
            return SentimentFilterResult(action="allow", reason="No headlines - allow", tone="neutral", tone_score=0.0, relevance=0.0, reduce_factor=1.0, headlines=[])

        # Clasifica cada headline y toma el peor (más negativo con alta relevancia)
        classified: list[tuple[Headline, Classification]] = [(h, classify(h.title, symbol)) for h in headlines]

        # Ordena por riesgo: negativo + relevante primero
        def risk_key(item):
            _, c = item
            # Más negativo y más relevante = mayor riesgo
            return (c.tone_score, -c.relevance)

        worst_headline, worst_cls = min(classified, key=risk_key)

        # Veto
        if worst_cls.tone_score <= self.veto_threshold and worst_cls.relevance >= self.relevance_veto:
            return SentimentFilterResult(
                action="veto",
                reason=f"VETO: {worst_cls.reason}",
                tone=worst_cls.tone,
                tone_score=worst_cls.tone_score,
                relevance=worst_cls.relevance,
                reduce_factor=0.0,
                headlines=headlines,
            )
        # Reduce
        if worst_cls.tone_score <= self.reduce_threshold and worst_cls.relevance >= self.relevance_reduce:
            return SentimentFilterResult(
                action="reduce",
                reason=f"REDUCE {int((1-self.reduce_factor)*100)}%: {worst_cls.reason}",
                tone=worst_cls.tone,
                tone_score=worst_cls.tone_score,
                relevance=worst_cls.relevance,
                reduce_factor=self.reduce_factor,
                headlines=headlines,
            )
        return SentimentFilterResult(
            action="allow",
            reason=f"ALLOW: {worst_cls.reason}",
            tone=worst_cls.tone,
            tone_score=worst_cls.tone_score,
            relevance=worst_cls.relevance,
            reduce_factor=1.0,
            headlines=headlines,
        )


# ---------------------------------------------------------------------------
# Eventos macro - pausa configurable
# ---------------------------------------------------------------------------
@dataclass
class MacroEvent:
    name: str
    event_time: datetime  # UTC
    pause_before_hours: int
    pause_after_hours: int


def is_macro_pause_active(events: list[MacroEvent], now: Optional[datetime] = None) -> tuple[bool, Optional[MacroEvent]]:
    """Retorna (is_paused, evento_que_causa_pausa). Fail-safe: si no hay eventos -> no pausa."""
    now = now or datetime.now(timezone.utc)
    for ev in events:
        start = ev.event_time - timedelta(hours=ev.pause_before_hours)
        end = ev.event_time + timedelta(hours=ev.pause_after_hours)
        if start <= now <= end:
            return True, ev
    return False, None


def parse_macro_events_from_env(env_value: str) -> list[MacroEvent]:
    """
    Parsea MACRO_EVENTS_JSON del .env.
    Formato esperado: '[{"name":"FOMC","time":"2026-09-17T18:00:00Z","before":2,"after":2}]'
    Si falla -> lista vacía (fail safe).
    """
    import json
    if not env_value or env_value.strip() == "":
        return []
    try:
        data = json.loads(env_value)
        events = []
        for item in data:
            try:
                t = item["time"]
                # Soporta Z
                evt_time = datetime.fromisoformat(t.replace("Z", "+00:00"))
                if evt_time.tzinfo is None:
                    evt_time = evt_time.replace(tzinfo=timezone.utc)
                events.append(MacroEvent(
                    name=item.get("name", "macro"),
                    event_time=evt_time,
                    pause_before_hours=int(item.get("before", 2)),
                    pause_after_hours=int(item.get("after", 2)),
                ))
            except Exception as e:
                logger.warning(f"Skipping macro event parse error {item}: {e}")
                continue
        return events
    except Exception as e:
        logger.warning(f"Failed to parse MACRO_EVENTS_JSON: {e} - no pause")
        return []
