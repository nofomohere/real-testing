#!/usr/bin/env python3
"""Fade Bot v2 — shadow paper trader for GitHub Actions.

Signals (both fade the crowd):
  A. MQL5 clusters: >=5 distinct providers open same pair+direction within 30 min,
     no opposition -> fade, exit after 8h.
  B. myfxbook sentiment extremes: retail >=80% one-sided on a pair -> fade, exit 8h.

Paper entries/exits at Yahoo prices; 1.5bps costs; Telegram notifications for
startup test, signals, closes, daily summary, and cookie-death alarms.
"""
import os, io, csv, json, re, subprocess, time, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, 'state.json')
LEDGER = os.path.join(HERE, 'ledger.jsonl')
LEDGER_MD = os.path.join(HERE, 'LEDGER.md')
MFB_JSON = os.path.join(HERE, 'mfb.json')
COOKIE = os.environ.get('MQL5_COOKIE', '').strip().replace('\n', '').replace('\r', '')
TG_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
HOLD_H = 8
COST_BPS = 1.5
CFG_PATH = os.path.join(HERE, 'config.json')
CFG = json.load(open(CFG_PATH)) if os.path.exists(CFG_PATH) else {"equity_usd": 10.0, "leverage": 50, "maintenance_margin": 0.01, "pairs": {}}
MC = int(CFG.get('min_cluster', 5))
NOTIONAL = CFG.get('equity_usd', 10.0) * CFG.get('leverage', 50)
LIQ_PCT = CFG.get('liq_adverse_pct', 1.0) / 100.0
VERBOSE = CFG.get('verbose', True)
PAIRS = list(CFG.get('pairs', {}).keys()) or ['EURUSD','GBPUSD','USDJPY','AUDUSD','USDCHF','USDCAD','NZDUSD','EURGBP','EURJPY','GBPJPY','AUDJPY','AUDCAD']
COSTS = CFG.get('pairs', {})
MFB_EXTREME = CFG.get('extreme_default', 80.0)   # fade when >=thr% one-sided
MFB_MIN_LOTS = 300.0 # ignore tiny pairs

def load_state():
    if os.path.exists(STATE):
        with open(STATE) as f:
            return json.load(f)
    return {"clusters_seen": {}, "active": {}, "mfb_last": {}, "chat_id": None, "tg_hello_sent": False, "alarm_sent": False}

def save_state(s):
    with open(STATE, 'w') as f:
        json.dump(s, f, indent=1)

def get(url, cookie=True, timeout=60):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36'})
    if cookie and COOKIE:
        req.add_header('Cookie', COOKIE)
    return urllib.request.urlopen(req, timeout=timeout).read()

# ---------------- Telegram ----------------
def tg_call(method, **params):
    if not TG_TOKEN:
        return None
    url = f'https://api.telegram.org/bot{TG_TOKEN}/{method}'
    try:
        data = urllib.parse.urlencode(params).encode()
        return json.loads(urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20).read())
    except Exception as e:
        print('tg error:', str(e)[:80])
        return None

def tg_send(text, st):
    cid = st.get('chat_id')
    if not cid or not TG_TOKEN:
        return False
    r = tg_call('sendMessage', chat_id=cid, text=text)
    return bool(r and r.get('ok'))

def tg_discover_chat(st):
    if st.get('chat_id') or not TG_TOKEN:
        return
    # fallback: the owner's chat id (harmless to be public; it's just a number)
    fallback = os.environ.get('TARGET_CHAT_ID') or '1192863557'
    r = tg_call('getUpdates', offset=-1, limit=1, timeout=0)
    try:
        upd = r.get('result') or []
        if upd:
            msg = upd[0].get('message') or upd[0].get('channel_post')
            if msg:
                st['chat_id'] = msg['chat']['id']
                print('chat id found:', st['chat_id'])
                return
    except Exception:
        pass
    st['chat_id'] = fallback
    print('chat id fallback:', fallback)

def tg_hello(st):
    if not st.get('tg_hello_sent') and st.get('chat_id'):
        ok = tg_send('✅ Fade Bot is ONLINE (GitHub Actions). Watching 39 MQL5 providers + myfxbook sentiment. Paper trades only — no real money. Signals, closes and daily summaries will arrive here.', st)
        if ok:
            st['tg_hello_sent'] = True

def tg_alarm(st, errs, total):
    if total > 0 and errs / total >= 0.9 and not st.get('alarm_sent'):
        if tg_send(f'⚠️ Fade Bot: MQL5 provider errors {errs}/{total} — the session cookie likely expired. Add a fresh MQL5_COOKIE secret.', st):
            st['alarm_sent'] = True
    elif total > 0 and errs / total < 0.5:
        st['alarm_sent'] = False

# ---------------- helpers ----------------
def norm_symbol(sym):
    s = sym.strip()
    if re.fullmatch(r'[A-Z]{6}', s): return s
    m = re.fullmatch(r'([A-Z]{6})[a-z0-9.#]+', s)
    return m.group(1) if m else None

def is_summer(dt):
    return 4 <= dt.month <= 10

def to_utc(raw, tzinfo):
    dt0 = datetime.strptime(raw, '%Y.%m.%d %H:%M:%S')
    off = tzinfo['summer'] if is_summer(dt0) else tzinfo['winter']
    return int((dt0 - timedelta(hours=off)).timestamp()*1000)

def fetch_opens(sid, tzinfo):
    try:
        data = get(f'https://www.mql5.com/en/signals/{sid}/export/history')
    except Exception as e:
        return None, str(e)[:60]
    text = data.decode('utf-8-sig', errors='ignore')
    if not text.startswith('Time'):
        return None, 'not-csv'
    rows = list(csv.reader(io.StringIO(text), delimiter=';'))
    out = []
    for r in rows[1:]:
        if len(r) != 10 or r[3] != 'In':
            continue
        sym = norm_symbol(r[1])
        if sym not in PAIRS:
            continue
        try:
            t = to_utc(r[0], tzinfo)
        except Exception:
            continue
        out.append((t, sym, r[2]))
    return out, None

def yahoo_close(pair):
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{pair}=X?interval=15m&range=1d'
    d = json.loads(get(url, cookie=False, timeout=30))
    res = d['chart']['result'][0]
    q = res['indicators']['quote'][0]
    ts, closes = res['timestamp'], q['close']
    for i in range(len(ts) - 1, -1, -1):
        if closes[i] is not None:
            return int(ts[i]) * 1000, float(closes[i])
    return None, None

def open_paper_trade(st, pair, fade_dir, source):
    bar_t, px = yahoo_close(pair)
    if not px:
        return None
    tid = f'{pair}-{fade_dir}-{bar_t}-{source}'
    tr = dict(pair=pair, fade_dir=fade_dir, open_t=bar_t, entry_px=px,
              exit_t=bar_t + HOLD_H * 3600 * 1000, source=source)
    st['active'][tid] = tr
    emoji = '🔥' if source == 'mql5' else '🧠'
    tg_send(f'{emoji} FADE SIGNAL ({source}): crowd one-sided on {pair} → paper {fade_dir.upper()} @ {px} (exit in {HOLD_H}h)', st)
    return tid

def yahoo_series(pair, start_ms, end_ms):
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{pair}=X?interval=15m&range=5d'
    try:
        d = json.loads(get(url, cookie=False, timeout=30))
        res = d['chart']['result'][0]
        ts, q = res['timestamp'], res['indicators']['quote'][0]
        out = []
        for i, t in enumerate(ts):
            tms = int(t) * 1000
            if start_ms <= tms <= end_ms and q['low'][i] is not None and q['high'][i] is not None:
                out.append((tms, float(q['low'][i]), float(q['high'][i])))
        return out
    except Exception:
        return []

def close_matured(st, now_ms):
    closed = []
    for tid, tr in list(st['active'].items()):
        if now_ms < tr['exit_t']:
            continue
        bar_t, px = yahoo_close(tr['pair'])
        if not px:
            continue
        sign = 1 if tr['fade_dir'] == 'Buy' else -1
        pnl_bps = sign * (px / tr['entry_px'] - 1) * 10000 - COSTS.get(tr['pair'], COST_BPS)
        # intrabar check: would the position have liquidated during the hold?
        series = yahoo_series(tr['pair'], tr['open_t'], tr['exit_t'])
        liquidated = False
        worst = 0.0
        for t, lo, hi in series:
            if tr['fade_dir'] == 'Buy':
                mae = (lo / tr['entry_px'] - 1)
            else:
                mae = (1 - hi / tr['entry_px'])
            worst = min(worst, mae)
            if mae <= -LIQ_PCT:
                liquidated = True
                break
        if liquidated:
            pnl_bps = -CFG['equity_usd'] * 10000 / NOTIONAL  # full margin lost
        pnl_usd = round(pnl_bps / 10000 * NOTIONAL, 4)
        rec = dict(trade=tid, pair=tr['pair'], fade_dir=tr['fade_dir'], source=tr.get('source','?'),
                   open_utc=datetime.fromtimestamp(tr['open_t']/1000, timezone.utc).isoformat(),
                   close_utc=datetime.fromtimestamp(bar_t/1000, timezone.utc).isoformat(),
                   entry_px=tr['entry_px'], exit_px=px, pnl_bps=round(pnl_bps, 2),
                   pnl_usd=pnl_usd, liquidated=liquidated,
                   equity_usd=CFG['equity_usd'], leverage=CFG['leverage'])
        with open(LEDGER, 'a') as f:
            f.write(json.dumps(rec) + '\n')
        closed.append(rec)
        del st['active'][tid]
        liq_txt = ' 💥LIQUIDATED' if liquidated else ''
        tg_send(f'✅ CLOSED ({tr.get("source","?")}): {tr["pair"]} {tr["fade_dir"].upper()} → {pnl_bps:+.1f} bps = {pnl_usd:+.3f} USD on ${CFG["equity_usd"]}@{CFG["leverage"]}x{liq_txt}', st)
    return closed

def write_ledger_md(st):
    if not os.path.exists(LEDGER):
        return
    recs = [json.loads(l) for l in open(LEDGER) if l.strip()]
    if not recs:
        return
    pnls = [r['pnl_bps'] for r in recs]
    wins = sum(1 for p in pnls if p > 0)
    with open(LEDGER_MD, 'w') as f:
        f.write('# Fade Bot — paper ledger\n\n')
        f.write(f'Updated: {datetime.now(timezone.utc).isoformat()}\n\n')
        usd = [r.get('pnl_usd', 0) for r in recs]
        liqs = sum(1 for r in recs if r.get('liquidated'))
        f.write(f'- Trades: **{len(pnls)}** | Avg: **{sum(pnls)/len(pnls):.1f} bps** (after {COST_BPS}bps costs) | Win: **{wins/len(pnls)*100:.0f}%** | Cumulative: **{sum(pnls):.1f} bps**\n')
        f.write(f'- Simulated money: equity **${CFG.get("equity_usd")}** at **{CFG.get("leverage")}x** → PnL **{sum(usd):+.3f} USD** | Liquidations: **{liqs}** | Open: **{len(st["active"])}**\n\n')
        f.write('| Trade | Pair | Dir | Src | Open (UTC) | Close (UTC) | PnL bps |\n|---|---|---|---|---|---|---|\n')
        for r in recs[-30:]:
            f.write(f"| {r['trade']} | {r['pair']} | {r['fade_dir']} | {r['source']} | {r['open_utc'][:16]} | {r['close_utc'][:16]} | {r['pnl_bps']:+.1f} |\n")

def status_heartbeat(st, mfb_count, errs, total, mfb_missing):
    now_ms = int(time.time() * 1000)
    last = st.get('status_last', 0)
    if now_ms - last < 4 * 3600 * 1000:
        return
    st['status_last'] = now_ms
    parts = []
    parts.append(f'📡 STATUS {datetime.now(timezone.utc).strftime("%H:%M")} UTC')
    parts.append(f'myfxbook pairs parsed: {mfb_count}' + (' (SCRAPER EMPTY — check workflow log)' if mfb_missing else ''))
    parts.append(f'MQL5 provider errors: {errs}/{total}')
    parts.append(f'active paper trades: {len(st["active"])}')
    if os.path.exists(LEDGER):
        recs = [json.loads(l) for l in open(LEDGER) if l.strip()]
        if recs:
            parts.append(f'ledger: {len(recs)} closed, cum {sum(r["pnl_bps"] for r in recs):+.1f} bps')
    tg_send('\n'.join(parts), st)

def cmd_reply(st, cmd):
    def ledger_stats():
        if not os.path.exists(LEDGER):
            return None
        recs = [json.loads(l) for l in open(LEDGER) if l.strip()]
        if not recs:
            return None
        pnls = [r['pnl_bps'] for r in recs]
        usd = [r.get('pnl_usd', 0) for r in recs]
        liqs = sum(1 for r in recs if r.get('liquidated'))
        return (f'📊 {len(recs)} trades | avg {sum(pnls)/len(pnls):+.1f} bps | cum {sum(pnls):+.1f} bps\n'
                f'💵 sim ${CFG.get("equity_usd")}@{CFG.get("leverage")}x: {sum(usd):+.3f} USD | liquidations: {liqs} | open: {len(st["active"])}')
    if cmd == '/status':
        mfb_n = 0
        if os.path.exists(MFB_JSON):
            try: mfb_n = len(json.load(open(MFB_JSON)))
            except Exception: pass
        return (f'📡 run: {datetime.now(timezone.utc).strftime("%H:%M")} UTC\n'
                f'myfxbook parsed: {mfb_n} pairs\n'
                f'active paper trades: {len(st["active"])}\n'
                f'ledger: ' + (ledger_stats() or 'no trades yet'))
    if cmd == '/stats':
        return ledger_stats() or 'no trades yet'
    if cmd == '/trades':
        if not os.path.exists(LEDGER):
            return 'no trades yet'
        recs = [json.loads(l) for l in open(LEDGER) if l.strip()][-5:]
        if not recs:
            return 'no trades yet'
        lines = [f'{r["pair"]} {r["fade_dir"]} {r["pnl_bps"]:+.1f}bps' + (' 💥LIQ' if r.get('liquidated') else '') for r in recs]
        return '\n'.join(lines)
    if cmd == '/pairs':
        items = [f'{p} ({COSTS.get(p, COST_BPS)}bps)' for p in PAIRS]
        return '\n'.join(items)
    if cmd == '/config':
        return f'equity ${CFG.get("equity_usd")} | {CFG.get("leverage")}x → notional ${NOTIONAL:,.0f} | liq at {CFG.get("liq_adverse_pct")}% adverse | mc={MC} | hold={HOLD_H}h'
    return ('commands: /status /stats /trades /pairs /config\n'
            'signals: 🔥=MQL5 cluster fade, 🧠=sentiment fade')

def tg_handle_commands(st):
    if not TG_TOKEN:
        return
    last = st.get('last_update_id', 0)
    r = tg_call('getUpdates', offset=last + 1, timeout=0)
    try:
        for upd in r.get('result') or []:
            st['last_update_id'] = upd['update_id']
            msg = upd.get('message') or {}
            text = (msg.get('text') or '').strip().lower()
            cid = msg.get('chat', {}).get('id')
            if not text.startswith('/'):
                continue
            reply = cmd_reply(st, text)
            runlog_line(cmd=text, replied=bool(reply))
            if reply:
                tg_call('sendMessage', chat_id=cid, text=reply)
    except Exception as e:
        print('cmd err:', str(e)[:100])

def runlog_line(**kw):
    line = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M') + ' | ' + ' | '.join(f'{k}={v}' for k, v in kw.items())
    with open(os.path.join(HERE, 'RUNLOG.md'), 'a') as f:
        f.write(line + '\n')
    try:
        lines = open(os.path.join(HERE, 'RUNLOG.md')).readlines()
        if len(lines) > 400:
            open(os.path.join(HERE, 'RUNLOG.md'), 'w').writelines(lines[-300:])
    except Exception:
        pass

def daily_summary(st):
    now = datetime.now(timezone.utc)
    if now.hour != 0:
        return
    if not os.path.exists(LEDGER):
        return
    key = now.strftime('%Y%m%d')
    if st.get('last_summary') == key:
        return
    recs = [json.loads(l) for l in open(LEDGER) if l.strip()]
    day = [r for r in recs if r['close_utc'][:10] == now.strftime('%Y-%m-%d')]
    if day:
        pnls = [r['pnl_bps'] for r in day]
        usd = [r.get('pnl_usd', 0) for r in day]
        tg_send(f'📊 DAILY: {len(day)} closes | avg {sum(pnls)/len(pnls):+.1f} bps | total {sum(pnls):+.1f} bps | ${sum(usd):+.3f} USD', st)
    st['last_summary'] = key

# ---------------- main ----------------
def main():
    tzmap = json.load(open(os.path.join(HERE, 'tz_map.json')))
    st = load_state()
    now_ms = int(time.time() * 1000)
    print(f'bot run at {datetime.now(timezone.utc).isoformat()}')
    tg_discover_chat(st)
    tg_hello(st)
    tg_handle_commands(st)

    errs = 0
    pool = []
    fail_fetch = 0
    fail_auth = 0
    if COOKIE:
        for sid, tzinfo in tzmap.items():
            opens, err = fetch_opens(sid, tzinfo)
            if opens is None:
                errs += 1
                if err == 'not-csv':
                    fail_auth += 1
                else:
                    fail_fetch += 1
                continue
            for t, sym, d in opens:
                if t >= now_ms - 4 * 3600 * 1000:
                    pool.append((t, sid, sym, d))
    else:
        errs = len(tzmap)
    print(f'MQL5 opens pooled: {len(pool)} | errors: {errs}/{len(tzmap)} (conn {fail_fetch}, auth {fail_auth})')
    tg_alarm(st, errs, len(tzmap))
    if fail_auth > 0 and not st.get('auth_alert_sent'):
        if tg_send(f'⚠️ AUTH: {fail_auth} provider exports returned the login page — MQL5 cookie is dead. Update MQL5_COOKIE secret.', st):
            st['auth_alert_sent'] = True
    elif fail_auth == 0:
        st['auth_alert_sent'] = False

    # A. MQL5 clusters
    evs = sorted(pool, key=lambda x: x[0])
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
            dirn = None
            if len(buys) >= MC and not sells:
                dirn = 'Buy'
            elif len(sells) >= MC and not buys:
                dirn = 'Sell'
            if dirn:
                key = f'{sym}|{dirn}|{t0 // (30 * 60000)}'
                if key not in st['clusters_seen']:
                    st['clusters_seen'][key] = True
                    fade = 'Sell' if dirn == 'Buy' else 'Buy'
                    open_paper_trade(st, sym, fade, 'mql5')
        i = j

    # B. myfxbook sentiment extremes
    if os.path.exists(MFB_JSON):
        try:
            mfb = json.load(open(MFB_JSON))
            for row in mfb:
                sym = row.get('symbol')
                if sym not in PAIRS:
                    continue
                pct = row.get('long_pct')
                lots = row.get('total_lots', 0)
                if pct is None or lots < MFB_MIN_LOTS:
                    continue
                thr = CFG.get('extreme', {}).get(sym, MFB_EXTREME)
                fade = None
                if pct >= thr:
                    fade = 'Sell'
                elif pct <= 100 - thr:
                    fade = 'Buy'
                if fade:
                    key = f'{sym}|{fade}'
                    last = st['mfb_last'].get(key, 0)
                    if now_ms - last > 24 * 3600 * 1000:
                        st['mfb_last'][key] = now_ms
                        open_paper_trade(st, sym, fade, 'myfxbook')
        except Exception as e:
            print('mfb parse error:', str(e)[:100])

    mfb_missing = False
    mfb_count = 0
    if os.path.exists(MFB_JSON):
        try:
            mfb_rows = json.load(open(MFB_JSON))
            mfb_count = len(mfb_rows)
            mfb_missing = (mfb_count == 0)
        except Exception:
            mfb_missing = True
    status_heartbeat(st, mfb_count, errs, len(tzmap), mfb_missing)
    closed = close_matured(st, now_ms)
    runlog_line(time=datetime.now(timezone.utc).strftime('%H:%M'), mfb=mfb_count, mql5_errs=f'{errs}/{len(tzmap)}',
                mql5_opens=len(pool), active=len(st['active']), closed=len(closed))
    if VERBOSE:
        # find the most extreme sentiment pair for the narration
        extreme = ''
        if os.path.exists(MFB_JSON):
            try:
                rows = json.load(open(MFB_JSON))
                best = max(rows, key=lambda r: abs(r.get('long_pct', 50) - 50), default=None)
                if best:
                    extreme = f' | most one-sided: {best["symbol"]} {best["long_pct"]}% long'
            except Exception:
                pass
        clusters_checked = len(st.get('clusters_seen', {}))
        line = (f'🔍 {datetime.now(timezone.utc).strftime("%H:%M")} UTC run | '
                f'mfb: {mfb_count} pairs{extreme} | '
                f'MQL5: {errs}/{len(tzmap)} errors, {len(pool)} opens | '
                f'clusters seen ever: {clusters_checked} | active: {len(st["active"])} | closed this run: {len(closed)}')
        tg_send(line, st)
    save_state(st)
    write_ledger_md(st)
    daily_summary(st)
    save_state(st)
    if closed:
        print(f'closed: {len(closed)}')
    print('done')

if __name__ == '__main__':
    main()
