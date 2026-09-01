"""Sentiment Module - News filter Fase 5 (solo reduce/veta)."""

from trading_bot.sentiment.provider import CryptoPanicProvider, NewsAPIProvider, Headline
from trading_bot.sentiment.classifier import classify, Classification
from trading_bot.sentiment.filter import SentimentFilter, SentimentFilterResult, is_macro_pause_active, parse_macro_events_from_env, MacroEvent

__all__ = [
    "CryptoPanicProvider", "NewsAPIProvider", "Headline",
    "classify", "Classification",
    "SentimentFilter", "SentimentFilterResult", "is_macro_pause_active", "parse_macro_events_from_env", "MacroEvent",
]
