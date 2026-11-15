"""PHASE 1 - read-only odds check. Places NO orders (the client has no order code).

Finds the current 15-minute BTC market, prints live Yes/No bid/ask and time remaining
every N seconds (aligned to the market's close, so samples land on 3:00, 2:45, ... 0:00),
and writes the same rows to logs/odds_check.csv.

    python -m kalshi_bot.odds_check --env prod --markets 3
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .client import ApiError, KalshiClient, Market
from .config import load_config

CSV_FIELDS = [
    "ts_utc", "ticker", "seconds_remaining", "status",
    "yes_bid", "yes_ask", "no_bid", "no_ask",
    "yes_ask_size", "no_ask_size", "no_ask_matches_100_minus_yes_bid",
    "yes_passes_threshold", "no_passes_threshold", "in_strategy_window",
]


def fmt(p: float | None) -> str:
    return "  --" if p is None else f"{p:4.0f}" if float(p).is_integer() else f"{p:5.1f}"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def check_row(m: Market, secs: float, threshold: float, start_secs: float, cutoff_secs: float) -> dict:
    consistent = None
    if m.yes_bid is not None and m.no_ask is not None:
        consistent = abs((100 - m.yes_bid) - m.no_ask) < 0.51
    return {
        "ts_utc": now_utc().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "ticker": m.ticker,
        "seconds_remaining": round(secs, 1),
        "status": m.status,
        "yes_bid": m.yes_bid, "yes_ask": m.yes_ask, "no_bid": m.no_bid, "no_ask": m.no_ask,
        "yes_ask_size": m.yes_ask_size, "no_ask_size": m.no_ask_size,
        "no_ask_matches_100_minus_yes_bid": consistent,
        "yes_passes_threshold": m.yes_ask is not None and m.yes_ask >= threshold,
        "no_passes_threshold": m.no_ask is not None and m.no_ask >= threshold,
        "in_strategy_window": cutoff_secs - 0.5 <= secs <= start_secs + 0.5,
    }


def print_row(r: dict) -> None:
    flags = []
    if r["yes_passes_threshold"]:
        flags.append("YES>=thr")
    if r["no_passes_threshold"]:
        flags.append("NO>=thr")
    if r["yes_ask"] is None or r["no_ask"] is None:
        flags.append("EMPTY-SIDE")
    if r["no_ask_matches_100_minus_yes_bid"] is False:
        flags.append("BOOK-MISMATCH")
    win = "W" if r["in_strategy_window"] else " "
    secs = r["seconds_remaining"]
    clock = f"{int(max(secs, 0)) // 60}:{int(max(secs, 0)) % 60:02d}"
    print(
        f"{r['ts_utc'][11:19]}Z {win} {r['ticker']} | {clock} left ({secs:6.1f}s) | "
        f"YES bid {fmt(r['yes_bid'])} ask {fmt(r['yes_ask'])} (x{r['yes_ask_size'] or 0:g}) | "
        f"NO bid {fmt(r['no_bid'])} ask {fmt(r['no_ask'])} (x{r['no_ask_size'] or 0:g}) | "
        f"{' '.join(flags)}",
        flush=True,
    )


def next_target_secs(secs: float, interval: float) -> float:
    """Next sample point on the close-aligned grid (..., 45, 30, 15, 0)."""
    target = math.floor(secs / interval) * interval
    if secs - target < 0.5:
        target -= interval
    return max(target, 0.0)


def pick_current(markets: list[Market]) -> Market | None:
    live = [m for m in markets if m.close_time > now_utc()]
    return min(live, key=lambda m: m.close_time) if live else None


def main(argv: list[str] | None = None) -> int:
    cfg = load_config()
    ap = argparse.ArgumentParser(description="Read-only Kalshi 15-min BTC odds check (Phase 1)")
    ap.add_argument("--env", choices=["demo", "prod"], default=cfg["market"]["default_env"],
                    help="which Kalshi host to READ prices from (no orders are ever placed)")
    ap.add_argument("--markets", type=int, default=3, help="number of FULL markets to observe")
    ap.add_argument("--max-minutes", type=float, default=70, help="hard time limit")
    ap.add_argument("--interval", type=float, default=cfg["timing"]["recheck_interval_seconds"])
    ap.add_argument("--base-url", default=None, help="override REST base URL (for testing)")
    ap.add_argument("--csv", default="logs/odds_check.csv")
    args = ap.parse_args(argv)

    series = cfg["market"]["series_ticker"]
    thr = cfg["strategy"]["entry_threshold_cents"]
    start_secs = cfg["timing"]["start_seconds_before_close"]
    cutoff_secs = cfg["timing"]["cutoff_seconds_before_close"]
    max_err = cfg["safety"]["max_api_errors_in_row"]

    client = KalshiClient(args.env, args.base_url)
    print(f"READ-ONLY odds check | env={args.env} | {client.base_url} | series={series}")
    print(f"threshold={thr}c  window={start_secs}s..{cutoff_secs}s (marked W)  interval={args.interval}s")
    try:
        skew = client.server_clock_skew()
        if skew is not None:
            print(f"clock skew vs Kalshi Date header: {skew:+.1f}s (header has 1s resolution)")
            if abs(skew) > 2.5:
                print("WARNING: local clock differs from Kalshi by >2.5s; time-remaining may be off")
    except ApiError as e:
        print(f"API check failed at startup: {e}")
        return 2

    Path(args.csv).parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + args.max_minutes * 60
    errors = 0
    current: Market | None = None
    first_secs: dict[str, float] = {}
    full_done = 0

    with open(args.csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        while full_done < args.markets and time.monotonic() < deadline:
            try:
                if current is None:
                    current = pick_current(client.get_open_markets(series))
                    if current is None:
                        print("no open market found yet; retrying in 5s", flush=True)
                        time.sleep(5)
                        continue
                    print(f"--- tracking {current.ticker} (closes {current.close_time:%H:%M:%S}Z) ---", flush=True)
                m = client.get_market(current.ticker)
                errors = 0
            except ApiError as e:
                errors += 1
                print(f"API ERROR ({errors}/{max_err}): {e}", flush=True)
                if errors >= max_err:
                    print("HALT: too many consecutive API errors")
                    return 2
                time.sleep(2)
                continue

            secs = (m.close_time - now_utc()).total_seconds()
            row = check_row(m, secs, thr, start_secs, cutoff_secs)
            print_row(row)
            w.writerow(row)
            f.flush()
            first_secs.setdefault(m.ticker, secs)

            if secs <= 0.5 or m.status not in ("open", "active"):
                full = first_secs[m.ticker] >= start_secs - args.interval
                print(f"--- {m.ticker} closed | full-window observation: {'yes' if full else 'no (joined late)'} "
                      f"| status={m.status} result={m.result or 'pending'} ---", flush=True)
                full_done += int(full)
                current = None
                time.sleep(2)
                continue

            target = next_target_secs(secs, args.interval)
            wake = m.close_time.timestamp() - target
            time.sleep(max(0.0, wake - time.time()))

    print(f"\nDone: observed {full_done} full market(s). Rows saved to {args.csv}")
    return 0 if full_done >= args.markets else 1


if __name__ == "__main__":
    sys.exit(main())
