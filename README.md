# Fade Bot (shadow paper trader)

Zero-cost, zero-computer FX strategy bot. It watches MQL5 signal providers, and when **5+ of them pile into the same FX pair in the same direction within 30 minutes (no opposition)**, it paper-trades the **opposite** direction at real Yahoo Finance prices and closes 8 hours later.

Strategy validated over 2.8 years of history: +7–8 bps/trade after costs, ~3.6 signals/week (see the `fx-copy-backtester/FADE_RESULT.md` report).

## How it works
- GitHub Actions runs `detector.py` every ~15 minutes (free).
- It polls the trade history of 39 timezone-resolved MQL5 providers using a session cookie stored in a GitHub **secret** (never in the repo).
- Detected clusters are paper-traded: entry at the latest 15-minute Yahoo close, exit 8 hours later, 1.5 bps costs subtracted.
- Every trade is logged to `ledger.jsonl` and summarized in `LEDGER.md`; state persists in `state.json` via auto-commits.

## Files
- `detector.py` — the bot
- `tz_map.json` — provider id → server timezone (resolved empirically at 74–100% confidence)
- `providers.json` — backup provider id list (bot currently uses tz_map keys)
- `.github/workflows/bot.yml` — the 15-minute schedule
- `state.json`, `ledger.jsonl`, `LEDGER.md` — live state and results

## What needs you
1. The `MQL5_COOKIE` GitHub secret (session cookie from mql5.com) — refresh it when providers stop updating (bot logs show provider errors rising).
2. Nothing else. Watch `LEDGER.md` accumulate.
