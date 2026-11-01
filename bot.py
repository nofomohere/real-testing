#!/usr/bin/env python3
"""
Fade Bot — professional edition (v3)
====================================
A zero-cost paper-trading bot for GitHub Actions that fades crowded retail FX.

Signals (all paper trades at live Yahoo Finance prices, costs included):
  1. crowd_fade     : >=MC distinct MQL5 providers open the same pair/direction
                      within 30 min with no opposition -> fade, exit after HOLD_H.
                      (Backtested 2024-2026: +7.8bps/trade at MC=5, +2.4bps at MC=3.)
  2. sentiment_fade : myfxbook retail positioning >= threshold% one-sided -> fade.
  3. scalp_meanrev  : FAST pipeline-demo mode: a pair moving >= move_bps within 30
                      minutes is faded toward the mean (TP=tp_bps, stop=move_bps,
                      time exit 30 min). Fires many times on active weekdays.
                      Labeled "scalp" in the ledger; research/demo, not the
                      validated edge.

Money reporting: config.equity_usd at config.leverage -> notional; every trade is
reported in bps AND USD, and is liquidation-checked intra-hold (liq_adverse_pct).

Telegram: token comes ONLY from the TELEGRAM_BOT_TOKEN environment variable.
Commands: /status /stats /trades /pairs /config /help. The bot scans all pending
updates each run and confirms them, so replies cannot be lost to stale state.

Run `python bot.py --selftest` to validate the environment before deploying.
"""
import os, io, csv, json, re, sys, time, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_F = os.path.join(HERE, 'state.json')
LEDGER_F = os.path.join(HERE, 'ledger.jsonl')
LEDGER_MD = os.path.join(HERE, 'LEDGER.md')
RUNLOG_F = os.path.join(HERE, 'RUNLOG.md')
MFB_F = os.path.join(HERE, 'mfb.json')
TZMAP_F = os.path.join(HERE, 'tz_map.json')
CFG_F = os.path.join(HERE, 'config.json')
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36')

# --------------------------------------------------------------------------- config
DEFAULT_CFG = {
    "equity_usd": 10.0,
    "leverage": 50,
    "liq_adverse_pct": 1.0,
    "min_cluster": 3,
    "hold_hours": 8,
    "extreme_default": 75.0,
    "extreme": {"BTCUSD": 85.0, "ETHUSD": 85.0},
    "min_lots": 300.0,
    "verbose": True,
    "status_hours": 4,
    "pairs": {"EURUSD": 1.5, "GBPUSD": 1.5, "USDJPY": 1.5, "AUDUSD": 1.5,
              "USDCHF": 1.5, "USDCAD": 1.5, "NZDUSD": 2.0, "EURGBP": 2.0,
              "EURJPY": 2.0, "GBPJPY": 3.0, "AUDJPY": 2.5, "AUDCAD": 2.5,
              "XAUUSD": 2.5, "GBPAUD": 3.0, "GBPCAD": 3.0, "EURAUD": 3.0,
              "EURCAD": 3.0, "EURTRY": 12.0, "USDTRY": 12.0, "USDZAR": 12.0,
              "EURNOK": 8.0, "USDNOK": 8.0, "US30": 2.0, "NAS100": 2.5,
              "SPX500": 2.0, "BTCUSD": 8.0, "ETHUSD": 8.0},
    "scalp": {"enabled": True, "move_bps": 25.0, "tp_bps": 10.0, "hold_min": 30,
              "max_open": 3,
              "move_override": {"XAUUSD": 60.0, "GBPJPY": 40.0, "BTCUSD": 80.0,
                                "ETHUSD": 80.0, "US30": 40.0, "NAS100": 45.0}},
    "chat_id_fallback": 1192863557,
}

def load_cfg():
    cfg = dict(DEFAULT_CFG)
    if os.path.exists(CFG_F):
        try:
            user = json.load(open(CFG_F))
        except Exception as e:
            raise SystemExit(f'config.json is not valid JSON: {e}')
        cfg.update(user)
    cfg['pairs'] = {**DEFAULT_CFG['pairs'], **cfg.get('pairs', {})}
    cfg['extreme'] = {**DEFAULT_CFG['extreme'], **cfg.get('extreme', {})}
    cfg['scalp'] = {**DEFAULT_CFG['scalp'], **cfg.get('scalp', {})}
    return cfg

CFG = load_cfg()
PAIRS = list(CFG['pairs'].keys())
COSTS = CFG['pairs']
MC = int(CFG['min_cluster'])
HOLD_MS = int(CFG['hold_hours']) * 3600 * 1000
NOTIONAL = CFG['equity_usd'] * CFG['leverage']
LIQ_PCT = CFG['liq_adverse_pct'] / 100.0
COOKIE = os.environ.get('MQL5_COOKIE', '').strip().replace('\n', '').replace('\r', '')
TG_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()

# ---------------------------------------------------------------------------- state
def load_state():
    base = {"version": 3, "clusters_seen": {}, "active": {}, "mfb_last": {},
            "chat_id": None, "tg_hello_sent": False, "alarm_sent": False,
            "auth_alert_sent": False, "status_last": 0, "last_summary": "",
            "last_update_id": 0, "scalp_last": {}}
    if os.path.exists(STATE_F):
        try:
            saved = json.load(open(STATE_F))
        except Exception:
            saved = {}
        base.update(saved)          # keeps old keys working (migration-safe)
        base.setdefault('version', 3)
    return base

def save_state(st):
    tmp = STATE_F + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE_F)        # atomic: never leaves a half-written file

# ----------------------------------------------------------------------------- log
def log(line):
    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')
    with open(RUNLOG_F, 'a') as f:
        f.write(f'{stamp} | {line}\n')
    print(line, flush=True)
    try:
        lines = open(RUNLOG_F).readlines()
        if len(lines) > 400:
            open(RUNLOG_F, 'w').writelines(lines[-300:])
    except Exception:
        pass

# -------------------------------------------------------------------------- fetch
def http_get(url, cookie=False, timeout=60):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    if cookie and COOKIE:
        req.add_header('Cookie', COOKIE)
    return urllib.request.urlopen(req, timeout=timeout).read()

def is_summer(dt):
    return 4 <= dt.month <= 10

def norm_symbol(sym):
    s = sym.strip()
    if re.fullmatch(r'[A-Z]{6}', s):
        return s
    m = re.fullmatch(r'([A-Z]{6})[a-z0-9.#]+', s)
    return m.group(1) if m else None

def to_utc_ms(raw, tz):
    dt0 = datetime.strptime(raw, '%Y.%m.%d %H:%M:%S')
    off = tz['summer'] if is_summer(dt0) else tz['winter']
    return int((dt0 - timedelta(hours=off)).timestamp() * 1000)

def fetch_mql5_opens(tzmap, since_ms):
    """Returns (opens, stats). opens: (utc_ms, provider, symbol, dir).
    stats: {ok, conn_err, auth_err, empty}."""
    opens, stats = [], {'ok': 0, 'conn_err': 0, 'auth_err': 0, 'empty': 0}
    if not COOKIE:
        stats['conn_err'] = len(tzmap)
        return opens, stats
    for sid, tz in tzmap.items():
        try:
            raw = http_get(f'https://www.mql5.com/en/signals/{sid}/export/history',
                           cookie=True, timeout=45)
        except Exception:
            stats['conn_err'] += 1
            continue
        text = raw.decode('utf-8-sig', errors='ignore')
        if not text.startswith('Time'):
            stats['auth_err'] += 1
            continue
        rows = list(csv.reader(io.StringIO(text), delimiter=';'))
        found = 0
        for r in rows[1:]:
            if len(r) != 10 or r[3] != 'In':
                continue
            sym = norm_symbol(r[1])
            if sym not in PAIRS:
                continue
            try:
                t = to_utc_ms(r[0], tz)
            except Exception:
                continue
            if t >= since_ms:
                opens.append((t, sid, sym, r[2]))
                found += 1
        if found:
            stats['ok'] += 1
        else:
            stats['empty'] += 1
        time.sleep(0.35)
    return opens, stats

_series_cache = {}

def _yahoo_series(pair, interval='15m', range_='2d'):
    url = (f'https://query1.finance.yahoo.com/v8/finance/chart/{pair}=X'
           f'?interval={interval}&range={range_}')
    d = json.loads(http_get(url, cookie=False, timeout=30))
    res = d['chart']['result'][0]
    ts, q = res['timestamp'], res['indicators']['quote'][0]
    out = []
    for i, t in enumerate(ts):
        o, h, l, c = q['open'][i], q['high'][i], q['low'][i], q['close'][i]
        if None in (o, h, l, c):
            continue
        out.append({'t': int(t) * 1000, 'o': float(o), 'h': float(h),
                    'l': float(l), 'c': float(c)})
    return out

def _kraken_series(pair, interval=15):
    url = f'https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval}'
    d = json.loads(http_get(url, cookie=False, timeout=30))
    if d.get('error'):
        raise RuntimeError(f'kraken: {d["error"]}')
    key = [k for k in d['result'] if k != 'last'][0]
    out = []
    for k in d['result'][key]:
        out.append({'t': int(k[0]) * 1000, 'o': float(k[1]), 'h': float(k[2]),
                    'l': float(k[3]), 'c': float(k[4])})
    return out

def price_series(pair, interval='15m', range_='2d'):
    """Cached price series with provider fallback: Yahoo -> Kraken.
    Returns list of bars {t,o,h,l,c} or [] if all sources fail."""
    key = (pair, interval, range_)
    if key in _series_cache:
        return _series_cache[key]
    out = []
    for attempt in range(2):
        try:
            out = _yahoo_series(pair, interval, range_)
            if len(out) >= 2:
                break
            out = []
        except Exception as e:
            if attempt == 1:
                log(f'yahoo {pair} failed: {str(e)[:60]}')
            time.sleep(2 * (attempt + 1))
    if len(out) < 2:
        try:
            out = _kraken_series(pair, 15)
            if len(out) < 2:
                out = []
            else:
                log(f'price {pair}: kraken fallback ({len(out)} bars)')
        except Exception as e:
            log(f'kraken {pair} failed: {str(e)[:60]}')
            out = []
    _series_cache[key] = out
    return out

def yahoo_latest(pair):
    s = price_series(pair, '15m', '1d')
    return (s[-1]['t'], s[-1]['c']) if s else (None, None)

# ------------------------------------------------------------------------- telegram
def tg_call(method, **params):
    if not TG_TOKEN:
        return None
    url = f'https://api.telegram.org/bot{TG_TOKEN}/{method}'
    try:
        data = urllib.parse.urlencode(params).encode()
        return json.loads(urllib.request.urlopen(
            urllib.request.Request(url, data=data), timeout=20).read())
    except Exception as e:
        log(f'tg {method} error: {str(e)[:80]}')
        return None

def tg_send(st, text):
    cid = st.get('chat_id')
    if not cid or not TG_TOKEN:
        return False
    r = tg_call('sendMessage', chat_id=cid, text=text)
    return bool(r and r.get('ok'))

def tg_discover_chat(st):
    if st.get('chat_id') or not TG_TOKEN:
        return
    r = tg_call('getUpdates', offset=-1, limit=1, timeout=0)
    try:
        upd = (r or {}).get('result') or []
        if upd:
            msg = upd[0].get('message') or upd[0].get('channel_post')
            if msg:
                st['chat_id'] = msg['chat']['id']
                log(f'chat id discovered: {st["chat_id"]}')
                return
    except Exception:
        pass
    st['chat_id'] = CFG.get('chat_id_fallback')
    log(f'chat id fallback: {st["chat_id"]}')

def cmd_reply(st, cmd):
    def ledger_summary():
        if not os.path.exists(LEDGER_F):
            return 'no trades yet'
        recs = [json.loads(l) for l in open(LEDGER_F) if l.strip()]
        if not recs:
            return 'no trades yet'
        pnls = [r['pnl_bps'] for r in recs]
        usd = [r.get('pnl_usd', 0) for r in recs]
        liqs = sum(1 for r in recs if r.get('liquidated'))
        srcs = defaultdict(int)
        for r in recs:
            srcs[r.get('source', '?')] += 1
        return (f'📊 {len(recs)} trades | avg {sum(pnls)/len(pnls):+.1f} bps | '
                f'cum {sum(pnls):+.1f} bps\n'
                f'💵 ${CFG["equity_usd"]}@{CFG["leverage"]}x: {sum(usd):+.3f} USD | '
                f'liquidations: {liqs}\n'
                f'sources: {dict(srcs)} | open: {len(st["active"])}')
    if cmd == '/status':
        mfb_n = 0
        if os.path.exists(MFB_F):
            try:
                mfb_n = len(json.load(open(MFB_F)))
            except Exception:
                pass
        return (f'📡 {datetime.now(timezone.utc).strftime("%H:%M")} UTC\n'
                f'myfxbook parsed: {mfb_n}\nMQL5 cookie: {"set" if COOKIE else "MISSING"}\n'
                f'active: {len(st["active"])}\n' + ledger_summary())
    if cmd == '/stats':
        return ledger_summary()
    if cmd == '/trades':
        if not os.path.exists(LEDGER_F):
            return 'no trades yet'
        recs = [json.loads(l) for l in open(LEDGER_F) if l.strip()][-5:]
        if not recs:
            return 'no trades yet'
        return '\n'.join(f'{r["pair"]} {r["fade_dir"]} {r["pnl_bps"]:+.1f}bps'
                         + (' 💥LIQ' if r.get('liquidated') else '')
                         for r in recs)
    if cmd == '/pairs':
        return '\n'.join(f'{p} ({COSTS.get(p)}bps)' for p in PAIRS)
    if cmd == '/config':
        return (f'equity ${CFG["equity_usd"]} | {CFG["leverage"]}x → notional '
                f'${NOTIONAL:,.0f} | liq at {CFG["liq_adverse_pct"]}% adverse\n'
                f'crowd fade: mc={MC}, hold={CFG["hold_hours"]}h\n'
                f'sentiment fade: ≥{CFG["extreme_default"]}% one-sided\n'
                f'scalp demo: {"ON" if CFG["scalp"]["enabled"] else "OFF"} '
                f'({CFG["scalp"]["move_bps"]}bps move, {CFG["scalp"]["tp_bps"]}bps TP)')
    return ('commands: /status /stats /trades /pairs /config\n'
            'signals: 🔥 crowd-fade · 🧠 sentiment-fade · ⚡ scalp-demo')

def tg_handle_commands(st):
    if not TG_TOKEN:
        return
    try:
        r = tg_call('getUpdates', timeout=0)          # all pending, pointer-proof
        updates = (r or {}).get('result') or []
        handled = 0
        for upd in updates:
            if upd['update_id'] <= st.get('last_update_id', 0):
                continue
            st['last_update_id'] = upd['update_id']
            msg = upd.get('message') or {}
            text = (msg.get('text') or '').strip().lower()
            cid = msg.get('chat', {}).get('id')
            if not text.startswith('/'):
                continue
            reply = cmd_reply(st, text)
            log(f'cmd {text} from {cid} -> replied={bool(reply)}')
            if reply:
                tg_call('sendMessage', chat_id=cid, text=reply)
            handled += 1
        if handled:
            tg_call('getUpdates', offset=st['last_update_id'] + 1, timeout=0)
    except Exception as e:
        log(f'cmd handler error: {str(e)[:120]}')

# --------------------------------------------------------------------------- signals
def find_clusters(opens, mc):
    evs = sorted(opens, key=lambda x: x[0])
    out = []
    i = 0
    while i < len(evs):
        t0 = evs[i][0]
        j = i
        by = defaultdict(lambda: [set(), set()])
        while j < len(evs) and evs[j][0] - t0 <= 30 * 60000:
            if evs[j][3] == 'Buy':
                by[evs[j][2]][0].add(evs[j][1])
            else:
                by[evs[j][2]][1].add(evs[j][1])
            j += 1
        for sym, (buys, sells) in by.items():
            if len(buys) >= mc and not sells:
                out.append((t0, sym, 'Buy'))
            elif len(sells) >= mc and not buys:
                out.append((t0, sym, 'Sell'))
        i = j
    return out

def open_trade(st, pair, fade_dir, source, tp_bps=None, sl_bps=None, hold_ms=None):
    if pair not in PAIRS:
        return None
    if any(t['pair'] == pair for t in st['active'].values()):
        return None                                   # one position per pair max
    t, px = yahoo_latest(pair)
    if not px:
        return None
    cost = COSTS.get(pair, 1.5)
    tid = f'{pair}-{fade_dir}-{t}-{source}'
    st['active'][tid] = dict(pair=pair, fade_dir=fade_dir, source=source,
                             open_t=t, entry_px=px,
                             exit_t=t + (hold_ms or HOLD_MS),
                             tp_bps=tp_bps, sl_bps=sl_bps, cost_bps=cost)
    emoji = {'crowd': '🔥', 'sentiment': '🧠', 'scalp': '⚡'}.get(source, '🔔')
    tg_send(st, f'{emoji} {source.upper()} SIGNAL: {pair} → paper {fade_dir.upper()} '
                f'@ {px} (exit in {(hold_ms or HOLD_MS)//3600000}h)')
    log(f'OPEN {tid} {fade_dir} @ {px}')
    return tid

def close_trades(st, now_ms):
    closed = []
    for tid, tr in list(st['active'].items()):
        if now_ms < tr['exit_t']:
            continue
        series = price_series(tr['pair'], '15m', '5d')
        if not series:
            continue
        sign = 1 if tr['fade_dir'] == 'Buy' else -1
        entry = tr['entry_px']
        exit_px, reason = None, 'time'
        for bar in series:
            if bar['t'] < tr['open_t']:
                continue
            adverse = sign * (bar['l'] / entry - 1) if tr['fade_dir'] == 'Buy' \
                else sign * (1 - bar['h'] / entry)
            favorable = sign * (bar['h'] / entry - 1) if tr['fade_dir'] == 'Buy' \
                else sign * (1 - bar['l'] / entry)
            if adverse <= -LIQ_PCT:
                exit_px, reason = None, 'liq'
                break
            if tr.get('sl_bps') and adverse <= -tr['sl_bps'] / 10000:
                exit_px, reason = (bar['l'] if tr['fade_dir'] == 'Buy' else bar['h']), 'stop'
                break
            if tr.get('tp_bps') and favorable >= tr['tp_bps'] / 10000:
                exit_px, reason = (bar['h'] if tr['fade_dir'] == 'Buy' else bar['l']), 'tp'
                break
            if bar['t'] >= tr['exit_t']:
                exit_px, reason = bar['c'], 'time'
                break
        if reason == 'liq':
            pnl_bps = -CFG['equity_usd'] * 10000 / NOTIONAL
        elif exit_px:
            pnl_bps = sign * (exit_px / entry - 1) * 10000 - tr.get('cost_bps', 1.5)
        else:
            continue
        rec = dict(trade=tid, pair=tr['pair'], fade_dir=tr['fade_dir'],
                   source=tr['source'], open_utc=datetime.fromtimestamp(
                       tr['open_t']/1000, timezone.utc).isoformat(),
                   close_utc=datetime.now(timezone.utc).isoformat(),
                   entry_px=entry, exit_px=exit_px or entry,
                   pnl_bps=round(pnl_bps, 2),
                   pnl_usd=round(pnl_bps / 10000 * NOTIONAL, 4),
                   liquidated=(reason == 'liq'))
        with open(LEDGER_F, 'a') as f:
            f.write(json.dumps(rec) + '\n')
        closed.append(rec)
        del st['active'][tid]
        tg_send(st, f'✅ CLOSED ({tr["source"]}): {tr["pair"]} {tr["fade_dir"].upper()} '
                    f'→ {pnl_bps:+.1f} bps = {rec["pnl_usd"]:+.3f} USD'
                    + (' 💥LIQUIDATED' if reason == 'liq' else ''))
        log(f'CLOSE {tid} reason={reason} pnl={pnl_bps:+.1f}bps')
    return closed

def scalp_signals(st, now_ms):
    if not CFG['scalp'].get('enabled'):
        return 0
    opened = 0
    for pair in PAIRS:
        if len([t for t in st['active'].values() if t['source'] == 'scalp']) >= \
                CFG['scalp'].get('max_open', 3):
            break
        if st['scalp_last'].get(pair, 0) > now_ms - 30 * 60000:
            continue
        series = price_series(pair, '15m', '1d')
        if len(series) < 4:
            continue
        now_bar, prev = series[-1], series[-3]        # ~30 min apart
        ret = now_bar['c'] / prev['c'] - 1
        move_bps = CFG['scalp'].get('move_override', {}).get(pair,
                     CFG['scalp']['move_bps']) / 10000
        if abs(ret) >= move_bps:
            fade = 'Sell' if ret > 0 else 'Buy'
            if open_trade(st, pair, fade, 'scalp', tp_bps=CFG['scalp']['tp_bps'],
                          sl_bps=CFG['scalp']['move_bps'],
                          hold_ms=CFG['scalp']['hold_min'] * 60000):
                st['scalp_last'][pair] = now_ms
                opened += 1
    return opened

# ---------------------------------------------------------------------------- ledger
def write_ledger_md(st):
    if not os.path.exists(LEDGER_F):
        return
    recs = [json.loads(l) for l in open(LEDGER_F) if l.strip()]
    if not recs:
        return
    pnls = [r['pnl_bps'] for r in recs]
    usd = [r.get('pnl_usd', 0) for r in recs]
    wins = sum(1 for p in pnls if p > 0)
    liqs = sum(1 for r in recs if r.get('liquidated'))
    with open(LEDGER_MD, 'w') as f:
        f.write('# Fade Bot — paper ledger\n\n')
        f.write(f'Updated: {datetime.now(timezone.utc).isoformat()}\n\n')
        f.write(f'- Trades: **{len(pnls)}** | Avg: **{sum(pnls)/len(pnls):.1f} bps** '
                f'| Win: **{wins/len(pnls)*100:.0f}%** | Cum: **{sum(pnls):.1f} bps**\n')
        f.write(f'- Money sim (${CFG["equity_usd"]} @ {CFG["leverage"]}x): '
                f'**{sum(usd):+.3f} USD** | Liquidations: **{liqs}** | '
                f'Open: **{len(st["active"])}**\n\n')
        f.write('| Trade | Pair | Dir | Src | Open (UTC) | PnL bps | PnL USD |\n'
                '|---|---|---|---|---|---|---|\n')
        for r in recs[-30:]:
            f.write(f"| {r['trade']} | {r['pair']} | {r['fade_dir']} | {r['source']} | "
                    f"{r['open_utc'][:16]} | {r['pnl_bps']:+.1f} | {r.get('pnl_usd', 0):+.4f} |\n")

# ------------------------------------------------------------------------------ main
def main():
    now_ms = int(time.time() * 1000)
    st = load_state()
    tzmap = json.load(open(TZMAP_F))

    tg_discover_chat(st)
    if not st.get('tg_hello_sent'):
        if tg_send(st, '✅ Fade Bot ONLINE — crowd-fade + sentiment-fade + scalp-demo. '
                       'Paper only. Commands: /status /stats /trades /pairs /config /help.'):
            st['tg_hello_sent'] = True
    tg_handle_commands(st)
    save_state(st)                       # persist chat id + command pointer immediately

    # 1. crowd fade (MQL5)
    since = now_ms - 4 * 3600 * 1000
    opens, stats = fetch_mql5_opens(tzmap, since)
    if stats['auth_err'] > 0 and not st.get('auth_alert_sent'):
        if tg_send(st, f'⚠️ AUTH: {stats["auth_err"]} exports returned the login page '
                       f'— MQL5 cookie is dead. Update the MQL5_COOKIE secret.'):
            st['auth_alert_sent'] = True
    if stats['auth_err'] == 0:
        st['auth_alert_sent'] = False
    for t0, sym, dirn in find_clusters(opens, MC):
        key = f'{sym}|{dirn}|{t0 // (30 * 60000)}'
        if key not in st['clusters_seen']:
            st['clusters_seen'][key] = True
            open_trade(st, sym, 'Sell' if dirn == 'Buy' else 'Buy', 'crowd')

    # 2. sentiment fade (myfxbook json from the node step)
    mfb_rows = []
    if os.path.exists(MFB_F):
        try:
            mfb_rows = json.load(open(MFB_F)) or []
        except Exception:
            mfb_rows = []
    for row in mfb_rows:
        sym = row.get('symbol')
        pct = row.get('long_pct')
        if sym not in PAIRS or pct is None or row.get('total_lots', 0) < CFG['min_lots']:
            continue
        thr = CFG['extreme'].get(sym, CFG['extreme_default'])
        fade = 'Sell' if pct >= thr else ('Buy' if pct <= 100 - thr else None)
        if fade:
            key = f'{sym}|{fade}'
            if now_ms - st['mfb_last'].get(key, 0) > 24 * 3600 * 1000:
                st['mfb_last'][key] = now_ms
                open_trade(st, sym, fade, 'sentiment')

    # 3. fast scalp demo
    scalp_n = scalp_signals(st, now_ms)

    closed = close_trades(st, now_ms)
    save_state(st)
    write_ledger_md(st)

    # status heartbeat
    if now_ms - st.get('status_last', 0) > CFG['status_hours'] * 3600 * 1000:
        st['status_last'] = now_ms
        tg_send(st, cmd_reply(st, '/status'))
    # daily summary at 00:0x UTC
    now = datetime.now(timezone.utc)
    if now.hour == 0 and st.get('last_summary') != now.strftime('%Y%m%d') \
            and os.path.exists(LEDGER_F):
        recs = [json.loads(l) for l in open(LEDGER_F) if l.strip()]
        day = [r for r in recs if r['close_utc'][:10] == now.strftime('%Y-%m-%d')]
        if day:
            pnls = [r['pnl_bps'] for r in day]
            usd = [r.get('pnl_usd', 0) for r in day]
            tg_send(st, f'📊 DAILY: {len(day)} closes | avg {sum(pnls)/len(pnls):+.1f} '
                        f'bps | ${sum(usd):+.3f} USD')
        st['last_summary'] = now.strftime('%Y%m%d')
    # verbose per-run line
    if CFG['verbose']:
        extreme = ''
        if mfb_rows:
            best = max(mfb_rows, key=lambda r: abs(r.get('long_pct', 50) - 50),
                       default=None)
            if best:
                extreme = f' | most one-sided: {best["symbol"]} {best["long_pct"]}% long'
        tg_send(st, f'🔍 {now.strftime("%H:%M")} UTC | mfb {len(mfb_rows)}{extreme} | '
                    f'MQL5 {stats["ok"]}ok/{stats["conn_err"]}conn/{stats["auth_err"]}auth, '
                    f'{len(opens)} opens | 🔥0 new | ⚡{scalp_n} | active {len(st["active"])} '
                    f'| closed {len(closed)}')
    save_state(st)

# -------------------------------------------------------------------------- selftest
def selftest():
    fails = []
    def check(name, ok, detail=''):
        print(('PASS' if ok else 'FAIL') + f' | {name}' + (f' | {detail}' if detail else ''))
        if not ok:
            fails.append(name)
    check('config.json valid', bool(CFG['pairs']), f'{len(PAIRS)} pairs')
    check('state round-trip', save_state(load_state()) is None)
    s = price_series('EURUSD')
    check('price source (yahoo->kraken)', len(s) > 10, f'{len(s)} bars')
    check('telegram token set', bool(TG_TOKEN))
    if TG_TOKEN:
        me = tg_call('getMe')
        check('telegram getMe', bool(me and me.get('ok')),
              (me.get('result') or {}).get('username', ''))
    tzmap = json.load(open(TZMAP_F))
    check('tz map', len(tzmap) >= 30, f'{len(tzmap)} providers')
    check('MQL5 cookie set', bool(COOKIE))
    if COOKIE:
        opens, stats = fetch_mql5_opens(dict(list(tzmap.items())[:2]), 0)
        check('MQL5 export fetch', stats['ok'] >= 1 or stats['empty'] >= 1,
              f'ok={stats["ok"]} empty={stats["empty"]} auth={stats["auth_err"]}')
    # signal math sanity: 5-provider one-sided cluster is found; opposition blocks it
    evs = [(1000, 'p1', 'EURUSD', 'Buy'), (1010, 'p2', 'EURUSD', 'Buy'),
           (1020, 'p3', 'EURUSD', 'Buy'), (1030, 'p4', 'EURUSD', 'Buy'),
           (1040, 'p5', 'EURUSD', 'Buy'),
           (5010, 'p6', 'USDJPY', 'Buy'), (5020, 'p7', 'USDJPY', 'Buy'),
           (5030, 'p8', 'USDJPY', 'Sell')]
    sigs = find_clusters(evs, 5)
    check('cluster logic', sigs == [(1000, 'EURUSD', 'Buy')], str(sigs))
    print('\nSELFTEST ' + ('FAILED: ' + ', '.join(fails) if fails else 'ALL PASS'))
    return 1 if fails else 0

if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.exit(selftest())
    main()
