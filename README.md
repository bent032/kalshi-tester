# Kalshi 15-minute BTC bot (work in progress)

**Status: Phase 1 only (read-only odds check). No order code exists yet.**

## Run Phase 1 on GitHub
1. Create a new GitHub repo and push these files to it.
2. Go to **Actions -> "Phase 1 - read-only odds check" -> Run workflow**.
   - `env`: `prod` reads real Kalshi prices (public data, no API key, no orders). `demo` reads the sandbox.
   - `markets`: how many full 15-minute markets to watch (default 3; the run takes up to ~1 hour).
3. Open the run and read the "Run read-only odds check" step, or download the `phase1-odds-check` artifact (CSV + log).

## Run locally
    pip install -r requirements.txt
    python -m kalshi_bot.odds_check --env prod --markets 3

## Reading the output
`W` marks samples inside the strategy window (3:00 down to the 30s cutoff). `YES>=thr` / `NO>=thr` means that side's
ask is at or above the 81c threshold in `config.toml` (nothing is bought in Phase 1). `BOOK-MISMATCH` means
NO ask != 100 - YES bid, which would indicate a data problem. `EMPTY-SIDE` means a missing ask.

All thresholds live in `config.toml`.
