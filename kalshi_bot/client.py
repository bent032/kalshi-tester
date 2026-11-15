"""Minimal READ-ONLY Kalshi client (Phase 1). It has no order methods at all.

Public market-data endpoints need no API key. Field names follow Kalshi's current
docs: prices are dollar strings (yes_ask_dollars = "0.8100"), times are ISO-8601.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import requests

BASE_URLS = {
    "prod": "https://external-api.kalshi.com/trade-api/v2",
    "demo": "https://external-api.demo.kalshi.co/trade-api/v2",
}


class ApiError(Exception):
    pass


def dollars_to_cents(value) -> float | None:
    """'0.8100' -> 81.0. Returns None for missing/blank values."""
    if value is None or value == "":
        return None
    try:
        return float(Decimal(str(value)) * 100)
    except InvalidOperation as e:
        raise ApiError(f"unparseable price {value!r}") from e


def _size(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(Decimal(str(value)))
    except InvalidOperation:
        return None


def parse_time(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Market:
    ticker: str
    status: str
    close_time: datetime
    yes_bid: float | None      # cents
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    yes_ask_size: float | None
    yes_bid_size: float | None
    no_ask_size: float | None
    no_bid_size: float | None
    result: str | None
    raw: dict

    @staticmethod
    def from_api(d: dict) -> "Market":
        try:
            return Market(
                ticker=d["ticker"],
                status=d.get("status", ""),
                close_time=parse_time(d["close_time"]),
                yes_bid=dollars_to_cents(d.get("yes_bid_dollars")),
                yes_ask=dollars_to_cents(d.get("yes_ask_dollars")),
                no_bid=dollars_to_cents(d.get("no_bid_dollars")),
                no_ask=dollars_to_cents(d.get("no_ask_dollars")),
                yes_ask_size=_size(d.get("yes_ask_size_fp")),
                yes_bid_size=_size(d.get("yes_bid_size_fp")),
                no_ask_size=_size(d.get("no_ask_size_fp")),
                no_bid_size=_size(d.get("no_bid_size_fp")),
                result=d.get("result") or None,
                raw=d,
            )
        except KeyError as e:
            raise ApiError(f"market payload missing field {e}") from e


class KalshiClient:
    def __init__(self, env: str = "demo", base_url: str | None = None, timeout: float = 10.0):
        if env not in BASE_URLS and not base_url:
            raise ValueError(f"unknown env {env!r}")
        self.env = env
        self.base_url = (base_url or BASE_URLS[env]).rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["Accept"] = "application/json"

    def _get(self, path: str, params: dict | None = None) -> tuple[dict, requests.Response]:
        url = f"{self.base_url}{path}"
        try:
            r = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as e:
            raise ApiError(f"request failed: {e}") from e
        if r.status_code != 200:
            raise ApiError(f"HTTP {r.status_code} from {path}: {r.text[:200]}")
        try:
            return r.json(), r
        except ValueError as e:
            raise ApiError(f"non-JSON response from {path}") from e

    def get_open_markets(self, series_ticker: str) -> list[Market]:
        data, _ = self._get("/markets", {"series_ticker": series_ticker, "status": "open", "limit": 20})
        return [Market.from_api(m) for m in data.get("markets", [])]

    def get_market(self, ticker: str) -> Market:
        data, _ = self._get(f"/markets/{ticker}")
        if "market" not in data:
            raise ApiError("response missing 'market'")
        return Market.from_api(data["market"])

    def server_clock_skew(self) -> float | None:
        """Local clock minus Kalshi's HTTP Date header, in seconds (1s resolution)."""
        from email.utils import parsedate_to_datetime

        _, r = self._get("/markets", {"limit": 1})
        d = r.headers.get("Date")
        if not d:
            return None
        server = parsedate_to_datetime(d)
        return (datetime.now(timezone.utc) - server).total_seconds()
