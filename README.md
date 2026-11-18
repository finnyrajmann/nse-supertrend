# NSE SUPERTREND

Daily paper-trading system on DigitalOcean Functions. Replaces 200emabb.

## Rules
- **Data:** Yahoo daily bars, run pre-market. Every fill price is the previous session's close; entry/exit dates are the run date.
- **Supertrend:** ATR period 10, factor 5.
- **Entry:** Supertrend flips up -> level = highest high of the 20 bars ending at the flip bar -> enter on the first close above the level. If the trend flips down first, the trade is skipped. If a close already cleared the level earlier, no late entry.
- **Exit:** Supertrend flips down (the Supertrend line is the trailing stop), or the EMA200 gate fails.
- **EMA200 gate** (checked at entry and daily for open positions): EMA200 today > 50 days back > 100 days back (strict), and price > EMA200. Failure on an open position exits with `EMA200_FAIL`.
- **Trading calendar:** `data/nse_holidays.csv` (same file and format as bb_ema_cross). The run skips weekends and listed holidays (a short holiday email goes out on weekday holidays). A stock whose newest bar is older than the last completed session is "stale": skipped for new entries, and listed in the email's Notices section; stale open positions are still evaluated and listed. Keep the file's `COVERS_THROUGH` row current — the email warns when it is close to expiring.
- **Invocation options:** `{"dry_run": true}` does everything except write to GitHub (email prefixed `[DRY RUN]`); `{"force": true}` runs on a non-session day.
- **Sizing:** Rs10,000 per trade.
- **Hit/miss:** PnL% > 3.0 = hit, otherwise miss.
- **Exit reasons:** `SUPERTREND_FLIP`, `EMA200_FAIL`, or `SUPERTREND_FLIP+EMA200_FAIL` when both fire the same day.

## Files (data/)
`watchlist_supertrend.csv` (from the screener), `positions_supertrend.csv`, `trade_log_hit_supertrend.csv`, `trade_log_miss_supertrend.csv`, `entry_snapshot_supertrend.csv`.

## Known Gaps / Improvements Backlog
- No fixed stop beyond the Supertrend line; a wide factor-5 band can give back a lot before it exits.
- Breakout level is fixed at the flip bar (not a rolling 20-bar high) — may differ from the Kite backtest.
- A run that misses the breakout day will not enter late.
- To verify later: NFO and Nifty50 watchlists.
