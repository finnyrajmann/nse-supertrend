"""
NSE SUPERTREND — DO Functions Entry Point
==========================================
Uses only requests + standard library (no pip installs needed).
- Yahoo Finance API for price data (OHLC)
- GitHub REST API for reading/writing CSV data
- Gmail SMTP for notifications

Design summary (Nov 2026):
- Runs once a day PRE-MARKET on the previous session's daily bars. Every
  price used (entry fill, exit fill, gap-down exit) is the previous
  session's close, because the run never sees the current day's data.
  Entry/exit dates are the run date.
- Supertrend: ATR period 10 (default), factor 5, hl2 basis, Wilder (RMA)
  ATR — same maths as TradingView's ta.supertrend.
- Entry (stateless — no pending file needed): the Supertrend is UP on the
  latest bar, it flipped up on some earlier bar (the "flip bar"), and the
  level is the highest HIGH of the 20 bars ending at (and including) the
  flip bar. Entry fires when the latest close is the FIRST close above
  that level since the flip. If the trend flips down before any close
  clears the level, the setup dies and the trade is skipped. If a close
  already cleared it on an earlier bar, the entry moment has passed and
  we do NOT enter late.
- Exit: Supertrend flips down (latest close below the Supertrend line).
  The Supertrend line IS the trailing stop, so stop and flip are one
  event. Exit fill = latest close (also covers gap-downs).
- EMA200 conditions, checked at ENTRY and again every day for OPEN
  positions (the watchlist is only refreshed monthly, so stale names can
  slip through):
      EMA200_today > EMA200_50_days_back > EMA200_100_days_back (strict)
      AND price > EMA200
  Same maths as the screener (SMA-seeded EMA, unrounded comparisons).
  Failure on an open position -> exit with reason EMA200_FAIL.
  If both exits fire the same day the reason is "SUPERTREND_FLIP+EMA200_FAIL".
- Hit/miss split: Hit = PnL% > 3.0, Miss = PnL% <= 3.0.
- File naming: system code as SUFFIX everywhere —
  watchlist_supertrend.csv, positions_supertrend.csv,
  trade_log_hit_supertrend.csv, trade_log_miss_supertrend.csv,
  entry_snapshot_supertrend.csv
"""

import os
import csv
import smtplib
import time
import base64
from io import StringIO
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
SYSTEM_CODE       = "supertrend"

ST_PERIOD         = 10      # Supertrend ATR period (default)
ST_FACTOR         = 5.0     # Supertrend multiplier
BREAKOUT_BARS     = 20      # entry level = highest high of this many bars up to the flip bar

EMA_LONG          = 200
CHECKPOINT_A      = 50      # trading days back
CHECKPOINT_B      = 100     # trading days back
MIN_HISTORY       = 300     # closes needed for the 3-checkpoint EMA200 check
DATA_PERIOD       = "2y"

POSITION_SIZE     = 10000
SLEEP             = 0.5
HIT_THRESHOLD_PCT = 3.0     # PnL% strictly greater than this -> hit, else -> miss

IST               = timezone(timedelta(hours=5, minutes=30))


def now_ist():
    return datetime.now(IST)


# ─────────────────────────────────────────────
# YAHOO FINANCE
# ─────────────────────────────────────────────
def fetch_price_bars(symbol, period=DATA_PERIOD):
    """
    Fetch daily OHLC bars for a symbol.
    Returns a list of dicts: {'date': date, 'open', 'high', 'low', 'close'}
    ordered oldest -> newest. Returns None on failure.

    Safety guard: if the newest bar is dated today (IST) and the market has
    not closed yet (before 16:00 IST), it is a partial intraday bar and is
    dropped, so a mid-day manual run can never use intraday prices.
    """
    ticker = symbol.upper().strip()
    if not ticker.startswith("^"):
        ticker = ticker + ".NS"

    params = {
        'range':    period,
        'interval': '1d',
        'events':   'history',
    }
    headers = {'User-Agent': 'Mozilla/5.0'}

    for host in ['query1', 'query2']:
        try:
            url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}"
            r = requests.get(url, params=params, headers=headers, timeout=15)
            data = r.json()
            result = data['chart']['result'][0]
            timestamps = result['timestamp']
            quote = result['indicators']['quote'][0]
            opens  = quote['open']
            highs  = quote['high']
            lows   = quote['low']
            closes = quote['close']

            bars = []
            for i, ts in enumerate(timestamps):
                c = closes[i]
                if c is None:
                    continue
                bars.append({
                    'date':  datetime.utcfromtimestamp(ts).date(),
                    'open':  opens[i] if opens[i] is not None else c,
                    'high':  highs[i] if highs[i] is not None else c,
                    'low':   lows[i] if lows[i] is not None else c,
                    'close': c,
                })

            if bars:
                t = now_ist()
                if bars[-1]['date'] == t.date() and t.hour < 16:
                    bars = bars[:-1]
            if bars:
                return bars
        except Exception:
            continue
    return None


# ─────────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────────
def calc_ema(values, period):
    """SMA-seeded EMA over a list (oldest -> newest). Unrounded. None if too short."""
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def calc_supertrend(bars, period=ST_PERIOD, factor=ST_FACTOR):
    """
    Supertrend (TradingView ta.supertrend maths: Wilder ATR on hl2 bands).
    Returns dict of lists aligned with bars:
      'dir'  : 1 = uptrend, -1 = downtrend, 0 = not yet defined
      'line' : the Supertrend line (lower band in uptrend, upper in downtrend)
    Returns None if there are not enough bars.
    """
    n = len(bars)
    if n < period + 2:
        return None

    highs  = [b['high'] for b in bars]
    lows   = [b['low'] for b in bars]
    closes = [b['close'] for b in bars]

    tr = [highs[0] - lows[0]]
    for i in range(1, n):
        tr.append(max(highs[i] - lows[i],
                      abs(highs[i] - closes[i - 1]),
                      abs(lows[i] - closes[i - 1])))

    atr = [None] * n
    atr[period - 1] = sum(tr[:period]) / period
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period

    direction = [0] * n
    line      = [None] * n
    f_upper   = [None] * n
    f_lower   = [None] * n

    start = period - 1
    for i in range(start, n):
        hl2 = (highs[i] + lows[i]) / 2
        b_upper = hl2 + factor * atr[i]
        b_lower = hl2 - factor * atr[i]

        if i == start:
            f_upper[i] = b_upper
            f_lower[i] = b_lower
            direction[i] = -1                      # Pine default: start in downtrend
        else:
            f_upper[i] = b_upper if (b_upper < f_upper[i - 1] or closes[i - 1] > f_upper[i - 1]) else f_upper[i - 1]
            f_lower[i] = b_lower if (b_lower > f_lower[i - 1] or closes[i - 1] < f_lower[i - 1]) else f_lower[i - 1]
            if direction[i - 1] == -1:
                direction[i] = 1 if closes[i] > f_upper[i] else -1
            else:
                direction[i] = -1 if closes[i] < f_lower[i] else 1

        line[i] = f_lower[i] if direction[i] == 1 else f_upper[i]

    return {'dir': direction, 'line': line, 'start': start}


def find_setup(bars, st):
    """
    Stateless setup finder. Returns None unless the Supertrend is UP on the
    latest bar AND a flip-up bar (down -> up) with at least BREAKOUT_BARS
    of history behind it can be found.

    Returns dict with:
      flip_idx, flip_date, level (highest high of the BREAKOUT_BARS bars
      ending at the flip bar, inclusive), first_break (index of the first
      close above level after the flip, or None).
    """
    n = len(bars)
    d = st['dir']
    last = n - 1
    if d[last] != 1:
        return None

    j = last
    while j - 1 >= st['start'] and d[j - 1] == 1:
        j -= 1
    if j - 1 < st['start'] or d[j - 1] != -1:
        return None                      # trend was already up at start of data; no observed flip
    flip = j
    if flip < BREAKOUT_BARS - 1:
        return None

    level = max(b['high'] for b in bars[flip - BREAKOUT_BARS + 1: flip + 1])

    first_break = None
    for k in range(flip + 1, n):
        if bars[k]['close'] > level:
            first_break = k
            break

    return {
        'flip_idx':    flip,
        'flip_date':   bars[flip]['date'],
        'level':       level,
        'first_break': first_break,
    }


def ema200_check(closes):
    """
    EMA200 uptrend gate, identical maths to the watchlist screener plus the
    price > EMA200 check:
      EMA200_today > EMA200_50d_back > EMA200_100d_back  (strict)  AND  price > EMA200
    Returns dict: ok (True/False/None), reason, ema200, ema_50back, ema_100back, price.
      ok is None when there is not enough history to evaluate.
    """
    if len(closes) < MIN_HISTORY:
        return {'ok': None, 'reason': 'insufficient history', 'ema200': None,
                'ema_50back': None, 'ema_100back': None, 'price': closes[-1] if closes else None}

    ema_today   = calc_ema(closes, EMA_LONG)
    ema_50back  = calc_ema(closes[:-CHECKPOINT_A], EMA_LONG)
    ema_100back = calc_ema(closes[:-CHECKPOINT_B], EMA_LONG)
    price       = closes[-1]

    fails = []
    if not (ema_today > ema_50back and ema_50back > ema_100back):
        fails.append(f"EMA200 structure broken ({ema_today:.2f} / {ema_50back:.2f} / {ema_100back:.2f})")
    if not (price > ema_today):
        fails.append(f"price {price:.2f} not above EMA200 {ema_today:.2f}")

    return {
        'ok':          len(fails) == 0,
        'reason':      '; '.join(fails),
        'ema200':      ema_today,
        'ema_50back':  ema_50back,
        'ema_100back': ema_100back,
        'price':       price,
    }


# ─────────────────────────────────────────────
# GITHUB REST API
# ─────────────────────────────────────────────
def github_get(repo, path, pat):
    """Read a file from GitHub. Returns (content, sha)."""
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {
        'Authorization': f'token {pat}',
        'Accept': 'application/vnd.github.v3+json',
    }
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    data    = r.json()
    content = base64.b64decode(data['content']).decode('utf-8')
    return content, data['sha']


def github_put(repo, path, pat, content, sha, message):
    """Write a file to GitHub."""
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {
        'Authorization': f'token {pat}',
        'Accept': 'application/vnd.github.v3+json',
    }
    payload = {
        'message': message,
        'content': base64.b64encode(content.encode('utf-8')).decode('utf-8'),
        'sha':     sha,
    }
    r = requests.put(url, headers=headers, json=payload, timeout=15)
    r.raise_for_status()
    return True


def parse_csv(content):
    reader = csv.DictReader(StringIO(content))
    return list(reader)


def to_csv(rows, fieldnames):
    out    = StringIO()
    writer = csv.DictWriter(out, fieldnames=fieldnames, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


# ─────────────────────────────────────────────
# EXIT MONITOR
# ─────────────────────────────────────────────
def run_exit(positions, hit_log, miss_log):
    """
    Check every open position:
      1. Supertrend flipped down on the latest bar  -> SUPERTREND_FLIP
      2. EMA200 structure / price>EMA200 now failing -> EMA200_FAIL
    Exit fill = latest close (previous session). Exit date = run date.
    Returns (exits, holds, remaining_positions, hit_log, miss_log)
    """
    exits         = []
    holds         = []
    new_positions = []
    hit_log       = list(hit_log)
    miss_log      = list(miss_log)
    today         = now_ist().date()

    for pos in positions:
        symbol      = pos['Symbol']
        entry_price = float(pos['EntryPrice'])
        quantity    = int(pos['Quantity'])
        entry_date  = datetime.strptime(pos['EntryDate'], '%Y-%m-%d').date()
        track_type  = pos['TrackType']
        capital     = round(entry_price * quantity, 2)
        days_held   = (today - entry_date).days

        bars = fetch_price_bars(symbol)
        time.sleep(SLEEP)
        st = calc_supertrend(bars) if bars else None
        if bars is None or st is None:
            print(f"  WARN: no usable data for {symbol} — position kept as is")
            new_positions.append(pos)
            continue

        closes = [b['close'] for b in bars]
        price  = round(closes[-1], 2)
        st_line = st['line'][-1]
        st_down = st['dir'][-1] == -1

        gate = ema200_check(closes)
        ema_fail = gate['ok'] is False

        reasons = []
        labels  = []
        if st_down:
            labels.append('SUPERTREND_FLIP')
            reasons.append(f"Supertrend turned down (close {price} below line {st_line:.2f})")
        if ema_fail:
            labels.append('EMA200_FAIL')
            reasons.append(gate['reason'])

        pnl     = round((price - entry_price) * quantity, 2)
        pnl_pct = round((price - entry_price) / entry_price * 100, 2)

        if labels:
            record = {
                'Symbol':     symbol,
                'EntryDate':  pos['EntryDate'],
                'EntryPrice': entry_price,
                'Quantity':   quantity,
                'Capital':    capital,
                'ExitDate':   today.strftime('%Y-%m-%d'),
                'ExitPrice':  price,
                'PnL':        pnl,
                'PnL%':       pnl_pct,
                'DaysHeld':   days_held,
                'ExitReason': f"{'+'.join(labels)} — {' | '.join(reasons)}",
                'TrackType':  track_type,
            }
            exits.append(record)
            if pnl_pct > HIT_THRESHOLD_PCT:
                hit_log.append(record)
            else:
                miss_log.append(record)
        else:
            new_positions.append(pos)
            holds.append({
                'Symbol':     symbol,
                'EntryPrice': entry_price,
                'Price':      price,
                'PnL':        pnl,
                'PnL%':       pnl_pct,
                'DaysHeld':   days_held,
                'STLine':     round(st_line, 2),
                'AboveLine%': round((price - st_line) / price * 100, 2),
            })

    return exits, holds, new_positions, hit_log, miss_log


# ─────────────────────────────────────────────
# ENTRY SCANNER
# ─────────────────────────────────────────────
def run_entry(watchlist, positions, entry_snapshots):
    """
    For each watchlist stock not already held: find the Supertrend flip-up
    setup, and enter if the latest close is the first close above the
    20-bar level AND the EMA200 gate passes today.

    Also collects 'armed' setups (flipped up, EMA200 gate passing, still
    waiting for the first close above the level) for the email only —
    nothing about them is stored.
    """
    open_symbols    = {p['Symbol'] for p in positions}
    new_entries     = []
    armed           = []
    entry_snapshots = list(entry_snapshots)
    entry_date      = now_ist().strftime('%Y-%m-%d')

    for row in watchlist:
        symbol = row['Symbol'].strip()
        if not symbol or symbol in open_symbols:
            continue

        bars = fetch_price_bars(symbol)
        time.sleep(SLEEP)
        if not bars or len(bars) < MIN_HISTORY:
            continue
        st = calc_supertrend(bars)
        if st is None:
            continue

        setup = find_setup(bars, st)
        if setup is None:
            continue

        last = len(bars) - 1
        closes = [b['close'] for b in bars]
        price  = round(closes[-1], 2)
        gate   = ema200_check(closes)
        if gate['ok'] is not True:
            if setup['first_break'] == last:
                print(f"  Skip {symbol}: breakout today but EMA200 gate failed ({gate['reason']})")
            continue

        if setup['first_break'] == last:
            quantity = max(1, int(POSITION_SIZE / price))
            positions.append({
                'Symbol':     symbol,
                'EntryDate':  entry_date,
                'EntryPrice': price,
                'Quantity':   quantity,
                'TrackType':  'Paper',
            })
            open_symbols.add(symbol)

            entry_snapshots.append({
                'Symbol':      symbol,
                'EntryDate':   entry_date,
                'Price':       price,
                'Supertrend':  round(st['line'][-1], 2),
                'FlipDate':    setup['flip_date'].strftime('%Y-%m-%d'),
                'Level20High': round(setup['level'], 2),
                'EMA200':      round(gate['ema200'], 2),
            })

            new_entries.append({
                'Symbol':     symbol,
                'Industry':   row.get('Industry', ''),
                'Price':      price,
                'Supertrend': round(st['line'][-1], 2),
                'FlipDate':   setup['flip_date'].strftime('%d %b'),
                'Level':      round(setup['level'], 2),
            })
            print(f"  Entry: {symbol} @ Rs.{price} — first close above 20-bar level "
                  f"{setup['level']:.2f} (flip {setup['flip_date']}, ST line {st['line'][-1]:.2f})")

        elif setup['first_break'] is None:
            armed.append({
                'Symbol':   symbol,
                'FlipDate': setup['flip_date'].strftime('%d %b'),
                'Level':    round(setup['level'], 2),
                'Price':    price,
                'ToLevel%': round((setup['level'] - price) / price * 100, 2),
            })

    armed.sort(key=lambda a: a['ToLevel%'])
    return new_entries, positions, entry_snapshots, armed


# ─────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────
def send_email(exits, entries, holds, armed, alltime_pnl=None, alltime_count=None,
               hit_count=None, miss_count=None):
    sender     = os.environ.get('GMAIL_SENDER')
    password   = os.environ.get('GMAIL_APP_PASSWORD')
    recipients = [r.strip() for r in os.environ.get('GMAIL_RECIPIENT', '').split(',') if r.strip()]
    repo_name  = os.environ.get('GITHUB_REPO')
    today      = now_ist().strftime('%d %b %Y')
    subject    = f"NSE SUPERTREND — {today} | {len(entries)} new | {len(holds)} open"

    def table_style():
        return 'border-collapse:collapse;width:100%;font-family:Arial,sans-serif;font-size:14px;'

    def th_style():
        return 'background:#2c3e50;color:#fff;padding:8px 12px;text-align:left;'

    def td_style(align='left'):
        return f'padding:7px 12px;border-bottom:1px solid #eee;text-align:{align};'

    def section_header(title):
        return f'<h3 style="color:#2c3e50;margin:24px 0 8px 0;">{title}</h3>'

    hits   = [e for e in exits if e['PnL%'] > HIT_THRESHOLD_PCT]
    misses = [e for e in exits if e['PnL%'] <= HIT_THRESHOLD_PCT]

    html = f'''
    <div style="font-family:Arial,sans-serif;max-width:700px;margin:0 auto;">
    <h2 style="background:#2c3e50;color:#fff;padding:14px 18px;margin:0;border-radius:4px 4px 0 0;">
        NSE SUPERTREND — {today}
    </h2>
    '''

    # EXITS
    html += section_header(
        f'Exits Today ({len(exits)}) &mdash; {len(hits)} hit / {len(misses)} miss'
    ) if exits else section_header('Exits: None today')
    if exits:
        html += f'<table style="{table_style()}"><thead><tr>'
        for col in ['', 'Symbol', 'P&L %', 'P&L Rs', 'Days', 'Reason']:
            html += f'<th style="{th_style()}">{col}</th>'
        html += '</tr></thead><tbody>'
        for r in exits:
            icon = '[H]' if r['PnL%'] > HIT_THRESHOLD_PCT else '[M]'
            html += f'''<tr>
                <td style="{td_style()}">{icon}</td>
                <td style="{td_style()}"><b>{r['Symbol']}</b></td>
                <td style="{td_style('right')}">{r['PnL%']:+.2f}%</td>
                <td style="{td_style('right')}">Rs.{r['PnL']:+.0f}</td>
                <td style="{td_style('right')}">{r['DaysHeld']}d</td>
                <td style="{td_style()}">{r['ExitReason']}</td>
            </tr>'''
        html += '</tbody></table>'

    # ENTRIES
    html += section_header(f'New Paper Entries ({len(entries)})') if entries else section_header('New Entries: None today')
    if entries:
        html += f'<table style="{table_style()}"><thead><tr>'
        for col in ['Symbol', 'Industry', 'Entry Rs', 'ST Line Rs', 'Flip', '20-bar High Rs']:
            html += f'<th style="{th_style()}">{col}</th>'
        html += '</tr></thead><tbody>'
        for e in entries:
            html += f'''<tr>
                <td style="{td_style()}"><b>{e['Symbol']}</b></td>
                <td style="{td_style()}">{e['Industry']}</td>
                <td style="{td_style('right')}">Rs.{e['Price']}</td>
                <td style="{td_style('right')}">Rs.{e['Supertrend']}</td>
                <td style="{td_style()}">{e['FlipDate']}</td>
                <td style="{td_style('right')}">Rs.{e['Level']}</td>
            </tr>'''
        html += '</tbody></table>'

    # OPEN POSITIONS
    if holds:
        total_pnl = sum(r['PnL'] for r in holds)
        pnl_color = '#27ae60' if total_pnl >= 0 else '#e74c3c'
        html += section_header(
            f'Open Positions ({len(holds)}) &nbsp;|&nbsp; '
            f'Total P&L: <span style="color:{pnl_color}">Rs.{total_pnl:+.0f}</span>'
        )
        html += f'<table style="{table_style()}"><thead><tr>'
        for col in ['', 'Symbol', 'Entry Rs', 'Price Rs', 'P&L %', 'P&L Rs', 'ST Line Rs', 'Above Line', 'Days']:
            html += f'<th style="{th_style()}">{col}</th>'
        html += '</tr></thead><tbody>'
        for r in holds:
            icon = '[+]' if r['PnL'] >= 0 else '[-]'
            html += f'''<tr>
                <td style="{td_style()}">{icon}</td>
                <td style="{td_style()}"><b>{r['Symbol']}</b></td>
                <td style="{td_style('right')}">Rs.{r['EntryPrice']:.2f}</td>
                <td style="{td_style('right')}">Rs.{r['Price']:.2f}</td>
                <td style="{td_style('right')}">{r['PnL%']:+.2f}%</td>
                <td style="{td_style('right')}">Rs.{r['PnL']:+.0f}</td>
                <td style="{td_style('right')}">Rs.{r['STLine']:.2f}</td>
                <td style="{td_style('right')}">{r['AboveLine%']:.2f}%</td>
                <td style="{td_style('right')}">{r['DaysHeld']}d</td>
            </tr>'''
        html += '</tbody></table>'
    else:
        html += section_header('Open Positions: None')

    # ARMED SETUPS (informational only — not stored anywhere)
    if armed:
        html += section_header(f'Armed — Flipped Up, Awaiting 20-bar Breakout ({len(armed)})')
        html += f'<table style="{table_style()}"><thead><tr>'
        for col in ['Symbol', 'Flip', 'Price Rs', 'Level Rs', 'To Level']:
            html += f'<th style="{th_style()}">{col}</th>'
        html += '</tr></thead><tbody>'
        for a in armed:
            html += f'''<tr>
                <td style="{td_style()}"><b>{a['Symbol']}</b></td>
                <td style="{td_style()}">{a['FlipDate']}</td>
                <td style="{td_style('right')}">Rs.{a['Price']:.2f}</td>
                <td style="{td_style('right')}">Rs.{a['Level']:.2f}</td>
                <td style="{td_style('right')}">{a['ToLevel%']:+.2f}%</td>
            </tr>'''
        html += '</tbody></table>'

    # CUMULATIVE TRADE LOG P&L
    if alltime_pnl is not None:
        at_color = '#27ae60' if alltime_pnl >= 0 else '#e74c3c'
        hit_rate = f'{(hit_count / alltime_count * 100):.0f}%' if alltime_count else 'N/A'
        html += section_header('All-Time Trade Log')
        html += f'''
        <table style="{table_style()}"><tbody>
            <tr>
                <td style="{td_style()}">Closed trades</td>
                <td style="{td_style('right')}">{alltime_count} ({hit_count} hit / {miss_count} miss, {hit_rate} hit rate)</td>
            </tr>
            <tr>
                <td style="{td_style()}">Cumulative P&amp;L</td>
                <td style="{td_style('right')}"><span style="color:{at_color}"><b>Rs.{alltime_pnl:+,.0f}</b></span></td>
            </tr>
        </tbody></table>
        '''

    # FOOTER
    html += f'''
    <p style="margin-top:24px;font-size:12px;color:#888;">
        <a href="https://github.com/{repo_name}/blob/master/data/trade_log_hit_{SYSTEM_CODE}.csv" style="color:#2c3e50;">
            View hit log
        </a> &nbsp;|&nbsp;
        <a href="https://github.com/{repo_name}/blob/master/data/trade_log_miss_{SYSTEM_CODE}.csv" style="color:#2c3e50;">
            View miss log
        </a><br>
        — NSE SUPERTREND (automated)
    </p>
    </div>
    '''

    msg = MIMEMultipart()
    msg['From']    = sender
    msg['To']      = ', '.join(recipients)
    msg['Subject'] = subject
    msg.attach(MIMEText(html, 'html'))

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(sender, password)
        server.sendmail(sender, recipients, msg.as_string())
    print(f"  Email sent to {', '.join(recipients)}")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main(args):
    print("\n" + "=" * 50)
    print("  NSE SUPERTREND — DO Functions Run")
    print("=" * 50)

    pat       = os.environ.get('GITHUB_PAT')
    repo_name = os.environ.get('GITHUB_REPO')

    pos_path      = f'data/positions_{SYSTEM_CODE}.csv'
    hit_log_path  = f'data/trade_log_hit_{SYSTEM_CODE}.csv'
    miss_log_path = f'data/trade_log_miss_{SYSTEM_CODE}.csv'
    wl_path       = f'data/watchlist_{SYSTEM_CODE}.csv'
    snap_path     = f'data/entry_snapshot_{SYSTEM_CODE}.csv'

    try:
        # Load data from GitHub
        print("\n[1/5] Loading data from GitHub...")
        pos_content, pos_sha       = github_get(repo_name, pos_path, pat)
        hitlog_content, hit_sha    = github_get(repo_name, hit_log_path, pat)
        misslog_content, miss_sha  = github_get(repo_name, miss_log_path, pat)
        wl_content, _              = github_get(repo_name, wl_path, pat)
        snap_content, snap_sha     = github_get(repo_name, snap_path, pat)

        positions       = parse_csv(pos_content)
        hit_log         = parse_csv(hitlog_content)
        miss_log        = parse_csv(misslog_content)
        watchlist       = parse_csv(wl_content)
        entry_snapshots = parse_csv(snap_content)
        print(f"      {len(positions)} open positions | {len(watchlist)} watchlist stocks")

        # Exit monitor
        print("\n[2/5] Exit Monitor...")
        exits, holds, positions, hit_log, miss_log = run_exit(positions, hit_log, miss_log)
        print(f"      {len(exits)} exit(s) | {len(holds)} holding")

        # Entry scanner
        print("\n[3/5] Entry Scanner...")
        entries, positions, entry_snapshots, armed = run_entry(watchlist, positions, entry_snapshots)
        print(f"      {len(entries)} new signal(s) | {len(armed)} armed setup(s)")

        # Add today's entries to holds for email
        for e in entries:
            holds.append({
                'Symbol':     e['Symbol'],
                'EntryPrice': e['Price'],
                'Price':      e['Price'],
                'PnL':        0.0,
                'PnL%':       0.0,
                'DaysHeld':   0,
                'STLine':     e['Supertrend'],
                'AboveLine%': round((e['Price'] - e['Supertrend']) / e['Price'] * 100, 2),
            })

        # Sync to GitHub
        print("\n[4/5] Syncing to GitHub...")
        commit_msg = f"Auto-update — {now_ist().strftime('%Y-%m-%d')}"

        pos_fields  = ['Symbol', 'EntryDate', 'EntryPrice', 'Quantity', 'TrackType']
        log_fields  = ['Symbol', 'EntryDate', 'EntryPrice', 'Quantity', 'Capital',
                       'ExitDate', 'ExitPrice', 'PnL', 'PnL%', 'DaysHeld',
                       'ExitReason', 'TrackType']
        snap_fields = ['Symbol', 'EntryDate', 'Price', 'Supertrend', 'FlipDate',
                       'Level20High', 'EMA200']

        github_put(repo_name, pos_path, pat,
                   to_csv(positions, pos_fields), pos_sha, commit_msg)
        github_put(repo_name, hit_log_path, pat,
                   to_csv(hit_log, log_fields), hit_sha, commit_msg)
        github_put(repo_name, miss_log_path, pat,
                   to_csv(miss_log, log_fields), miss_sha, commit_msg)
        github_put(repo_name, snap_path, pat,
                   to_csv(entry_snapshots, snap_fields), snap_sha, commit_msg)

        # Cumulative trade-log P&L (all closed trades to date, hit + miss)
        alltime_pnl = (sum(float(r['PnL']) for r in hit_log) +
                       sum(float(r['PnL']) for r in miss_log))
        alltime_count = len(hit_log) + len(miss_log)

        # Send email
        print("\n[5/5] Sending email...")
        send_email(exits, entries, holds, armed, alltime_pnl, alltime_count,
                   len(hit_log), len(miss_log))

        print("\n  Done.\n")
        return {"statusCode": 200, "body": "Pipeline complete"}

    except Exception as e:
        import traceback
        print(f"\n  ERROR: {str(e)}")
        print(traceback.format_exc())
        return {"statusCode": 500, "body": str(e)}
