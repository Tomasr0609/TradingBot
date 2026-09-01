"""News providers - CryptoPanic (default) + NewsAPI fallback."""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional
import httpx

logger = logging.getLogger(__name__)


@dataclass
class Headline:
    title: str
    source: str
    published_at: datetime
    url: str
    currencies: list[str]
    raw: dict


class CryptoPanicProvider:
    """Fetch crypto headlines from CryptoPanic (free tier, no key required for public)."""

    BASE_URL = "https://cryptopanic.com/api/v1/posts/"

    def __init__(self, auth_token: Optional[str] = None, http_client: Optional[httpx.AsyncClient] = None):
        self.auth_token = auth_token
        self._client = http_client

    async def fetch_recent(self, symbol: str = "BTC", hours: int = 24, limit: int = 20) -> list[Headline]:
        """Fetch headlines for symbol within last N hours. Fail-safe: returns [] on error."""
        # Normalize BTC/USDT -> BTC
        currency = symbol.split("/")[0].upper()
        params = {
            "currencies": currency,
            "public": "true",
            "kind": "news",
        }
        if self.auth_token:
            params["auth_token"] = self.auth_token

        client = self._client or httpx.AsyncClient(timeout=10)
        close = self._client is None
        try:
            resp = await client.get(self.BASE_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])[:limit]
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            headlines = []
            for r in results:
                try:
                    pub = r.get("published_at") or r.get("created_at")
                    published = datetime.fromisoformat(pub.replace("Z", "+00:00")) if pub else datetime.now(timezone.utc)
                    if published < cutoff:
                        continue
                    headlines.append(Headline(
                        title=r.get("title", ""),
                        source=r.get("source", {}).get("title", "unknown") if isinstance(r.get("source"), dict) else str(r.get("source")),
                        published_at=published,
                        url=r.get("url", ""),
                        currencies=[c.get("code", "") for c in r.get("currencies", [])] if r.get("currencies") else [currency],
                        raw=r,
                    ))
                except Exception as e:
                    logger.warning(f"Skipping headline parse error: {e}")
                    continue
            logger.info(f"CryptoPanic {currency}: {len(headlines)} headlines in last {hours}h")
            return headlines
        except Exception as e:
            logger.warning(f"CryptoPanic fetch failed for {currency}: {e} - fail safe empty")
            return []
        finally:
            if close:
                await client.aclose()


class NewsAPIProvider:
    """Alternative: NewsAPI (requires key)."""

    BASE_URL = "https://newsapi.org/v2/everything"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def fetch_recent(self, symbol: str = "BTC", hours: int = 24, limit: int = 20) -> list[Headline]:
        if not self.api_key:
            return []
        currency = symbol.split("/")[0]
        query = f"{currency} crypto"
        params = {"q": query, "language": "en", "sortBy": "publishedAt", "pageSize": limit}
        headers = {"X-Api-Key": self.api_key}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(self.BASE_URL, params=params, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                headlines = []
                for a in data.get("articles", [])[:limit]:
                    headlines.append(Headline(
                        title=a.get("title", ""),
                        source=a.get("source", {}).get("name", "newsapi"),
                        published_at=datetime.fromisoformat(a["publishedAt"].replace("Z", "+00:00")),
                        url=a.get("url", ""),
                        currencies=[currency],
                        raw=a,
                    ))
                return headlines
        except Exception as e:
            logger.warning(f"NewsAPI fetch failed: {e}")
            return []
