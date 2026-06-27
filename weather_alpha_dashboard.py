#!/usr/bin/env python3
"""
🌦 Weather Alpha Dashboard — Professional Signal & Portfolio Monitor

Shows:
  - Summary cards (signals, positions, PnL, win rate)
  - Active signals heat map (edge vs confidence)
  - Current positions with real-time PnL
  - Historical performance & equity curve
  - Market discovery stats

Usage:
  python3 weather_alpha_dashboard.py             # default port 8770
  python3 weather_alpha_dashboard.py --port 8771
"""

import sqlite3, json, time, sys, math
from pathlib import Path
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "weather_alpha_v3.db"
PORT = 8770
REFRESH_SEC = 30

HKT = timezone(timedelta(hours=8))

PAGE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🌦 Weather Alpha __VERSION__</title>
<style>
  :root {
    --bg: #0d1117;
    --card-bg: #161b22;
    --border: #30363d;
    --text: #c9d1d9;
    --muted: #8b949e;
    --green: #3fb950;
    --red: #f85149;
    --blue: #58a6ff;
    --orange: #d2991d;
    --purple: #a371f7;
    --cyan: #39d2c0;
    --pink: #db61a2;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { 
    background: var(--bg); color: var(--text); 
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    padding: 20px; max-width: 1500px; margin: 0 auto;
  }
  h1 { font-size: 22px; margin-bottom: 16px; display: flex; align-items: center; gap: 8px; }
  h2 { font-size: 16px; margin: 24px 0 12px; color: var(--muted); display: flex; align-items: center; gap: 8px; }
  h1 .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--green); display: inline-block; animation: pulse 2s infinite; }
  h1 .dot-stale { background: var(--orange); }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }

  /* ── Summary Cards ── */
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin-bottom: 20px; }
  .card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; position: relative; }
  .card .label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; }
  .card .value { font-size: 26px; font-weight: 700; }
  .card .sub { font-size: 11px; color: var(--muted); margin-top: 2px; }
  .val-good { color: var(--green); }
  .val-bad { color: var(--red); }
  .val-neutral { color: var(--blue); }
  .val-orange { color: var(--orange); }

  /* ── Tier badges ── */
  .tier { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 700; }
  .tier-A { background: rgba(63,185,80,0.2); color: var(--green); }
  .tier-B { background: rgba(88,166,255,0.2); color: var(--blue); }
  .tier-C { background: rgba(210,153,29,0.2); color: var(--orange); }
  .tier-D { background: rgba(139,148,158,0.15); color: var(--muted); }

  /* ── Signal badges ── */
  .sig-buy { background: rgba(63,185,80,0.15); color: var(--green); }
  .sig-sell { background: rgba(248,81,73,0.15); color: var(--red); }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
  .badge-open { background: rgba(88,166,255,0.15); color: var(--blue); }
  .badge-win { background: rgba(63,185,80,0.15); color: var(--green); }
  .badge-loss { background: rgba(248,81,73,0.15); color: var(--red); }

  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; padding: 10px 12px; border-bottom: 1px solid var(--border); color: var(--muted); font-weight: 500; font-size: 11px; text-transform: uppercase; position: sticky; top: 0; background: var(--bg); z-index: 1; }
  td { padding: 8px 12px; border-bottom: 1px solid var(--border); }
  tr:hover { background: #1c2128; }
  .pnl-pos { color: var(--green); font-weight: 600; }
  .pnl-neg { color: var(--red); font-weight: 600; }

  /* ── Edge bar ── */
  .edge-bar { display: inline-block; min-width: 120px; height: 18px; background: #21262d; border-radius: 4px; overflow: hidden; position: relative; vertical-align: middle; margin: 0 6px; }
  .edge-bar-fill { height: 100%; border-radius: 4px; transition: width 0.3s; }
  .edge-bar-label { position: absolute; width: 100%; text-align: center; line-height: 18px; font-size: 10px; font-weight: 600; color: #fff; text-shadow: 0 0 3px #000; }

  .refresh { font-size: 11px; color: var(--muted); text-align: right; margin-top: 8px; }
</style>
</head>
<body>

<h1><span class="dot" id="status-dot"></span>🌦 Weather Alpha __VERSION__ — Production Dashboard</h1>

<div class="cards" id="cards"></div>
<div class="cards" id="breakdown-cards" style="margin-top:4px;">
  <div class="card" style="border-left:2px solid var(--green);"><div class="label">BUY NO 战绩</div><div class="value val-good" id="bn-pnl">—</div><div class="sub"><span id="bn-detail">—</span></div></div>
  <div class="card" style="border-left:2px solid var(--red);"><div class="label">BUY YES 战绩</div><div class="value val-bad" id="by-pnl">—</div><div class="sub"><span id="by-detail">—</span></div></div>
</div>

<!-- Signals -->
<h2>⚡ Latest Signals <span style="font-size:12px;font-weight:400;color:var(--muted);" id="signal-count"></span></h2>
<table>
<thead>
  <tr>
    <th>Time</th><th>Signal</th><th>Tier</th><th>City</th><th>Dir</th><th>Market</th>
    <th style="width:200px;">Edge</th><th>Conf</th><th>Mkt%</th><th>Mdl%</th><th>Size</th><th>Vol</th><th>Link</th>
  </tr>
</thead>
<tbody id="signals-tbody"></tbody>
</table>

<!-- Positions -->
<h2>📊 Open Positions <span style="font-size:12px;font-weight:400;color:var(--muted);" id="pos-count"></span></h2>
<table>
<thead>
  <tr><th>Opened</th><th>City</th><th>Signal</th><th>Tier</th><th>Entry</th><th>Live</th><th>Float PnL</th><th>Shares</th><th>Cost</th><th>Market</th></tr>
</thead>
<tbody id="pos-tbody"></tbody>
</table>

<!-- Resolved Positions -->
<h2>✅ Settled Positions <span style="font-size:12px;font-weight:400;color:var(--muted);" id="resolved-count"></span></h2>
<table>
<thead>
  <tr><th>Resolved</th><th>City</th><th>Signal</th><th>Entry</th><th>Outcome</th><th>PnL</th><th>Shares</th><th>Cost</th><th>Market</th></tr>
</thead>
<tbody id="resolved-tbody"></tbody>
</table>

<div class="refresh" id="refresh">Loading...</div>

<script>
const REFRESH = __REFRESH_SEC__;

function fmt(n, d) { 
  if (typeof n !== 'number' || isNaN(n)) return '—';
  return '$' + n.toFixed(d || 2); 
}
function fmtPnL(n) {
  if (typeof n !== 'number' || isNaN(n)) return '—';
  const sign = n >= 0 ? '+' : '';
  return sign + '$' + n.toFixed(2);
}
function pct(n) {
  if (typeof n !== 'number' || isNaN(n)) return '—';
  return (n * 100).toFixed(1) + '%';
}
function fmtEdge(e) {
  if (typeof e !== 'number' || isNaN(e)) return '—';
  const sign = e >= 0 ? '+' : '';
  const pct = (e * 100).toFixed(1);
  const absPct = Math.abs(e * 100);
  const color = e >= 0.10 ? '#3fb950' : e >= 0.05 ? '#58a6ff' : e <= -0.10 ? '#f85149' : e <= -0.05 ? '#d2991d' : '#8b949e';
  const barW = Math.min(Math.abs(e) * 250, 100);
  const barColor = e > 0 ? '#3fb950' : '#f85149';
  return `<span style="color:${color};font-weight:600;width:50px;display:inline-block;">${sign}${pct}%</span>
    <span class="edge-bar"><span class="edge-bar-fill" style="width:${barW}%;background:${barColor};"></span></span>`;
}

function renderResolved(positions) {
  document.getElementById('resolved-count').textContent = '(' + positions.length + ' settled)';
  const tbody = document.getElementById('resolved-tbody');
  if (!positions.length) {
    tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:var(--muted);padding:24px;">暂无结算 — 等待市场到期</td></tr>';
    return;
  }
  tbody.innerHTML = positions.map(p => {
    const sigClass = p.signal === 'BUY YES' ? 'sig-buy' : 'sig-sell';
    const pmUrl = p.slug ? 'https://polymarket.com/market/' + p.slug : '';
    const marketCell = pmUrl
      ? '<a href="' + pmUrl + '" target="_blank" style="color:var(--blue);text-decoration:none;" title="Open in Polymarket">' + (p.question||'').substring(0,45) + '</a>'
      : (p.question||'').substring(0,45);
    const pnl = p.resolved_pnl || 0;
    const pnlCls = pnl > 0 ? 'pnl-pos' : pnl < 0 ? 'pnl-neg' : '';
    const outcomeEmoji = p.outcome === 'YES' ? '🟢 YES' : p.outcome === 'NO' ? '🔴 NO' : (p.outcome || '—');
    return '<tr>'
      + '<td>' + (p.resolved_at ? p.resolved_at.slice(0,16) : '—') + '</td>'
      + '<td>' + (p.city || '—') + '</td>'
      + '<td><span class="badge ' + sigClass + '">' + (p.signal || '—') + '</span></td>'
      + '<td>$' + ((p.entry_price||0).toFixed(4)) + '</td>'
      + '<td>' + outcomeEmoji + '</td>'
      + '<td class="' + pnlCls + '">' + fmtPnL(pnl) + '</td>'
      + '<td>' + ((p.shares||0).toLocaleString()) + '</td>'
      + '<td>' + fmt(p.cost) + '</td>'
      + '<td title="' + (p.question||'') + '">' + marketCell + '</td>'
      + '</tr>';
  }).join('');
}

function renderCards(data) {
  const s = data.stats || {};
  const pnl = s.realized_pnl || 0;
  const wr = s.win_rate || 0;
  const wins = s.wins || 0;
  const losses = s.losses || 0;
  const resolved = s.resolved || 0;
  const deployed = s.deployed || 0;
  const initial = s.initial_capital || 0;
  const available = s.available || 0;
  const deployPct = initial > 0 ? (deployed / initial * 100).toFixed(0) : 0;
  const availCls = available >= (initial * 0.2) ? 'val-good' : (available > 0 ? 'val-orange' : 'val-bad');
  document.getElementById('cards').innerHTML = `
    <div class="card"><div class="label">💰 初始资金</div><div class="value val-neutral">${fmt(initial,0)}</div><div class="sub">Weather Alpha</div></div>
    <div class="card"><div class="label">💵 可用余额</div><div class="value ${availCls}">${fmt(available)}</div><div class="sub">${deployPct}% 已部署</div></div>
    <div class="card"><div class="label">📊 已部署</div><div class="value val-blue" style="color:var(--blue);">${fmt(deployed)}</div><div class="sub">${s.open || 0} 个持仓</div></div>
    <div class="card"><div class="label">已实现 PnL</div><div class="value ${pnl>=0?'val-good':'val-bad'}">${fmtPnL(pnl)}</div><div class="sub">${wins}W / ${losses}L</div></div>
    <div class="card"><div class="label">胜率</div><div class="value ${wr>=50?'val-good':'val-bad'}">${wr}%</div><div class="sub">结算 ${resolved} 笔</div></div>
    <div class="card"><div class="label">总信号</div><div class="value val-neutral">${s.total_signals || 0}</div><div class="sub">最近 ${data.current_signals || 0} 个</div></div>
  `;
}

function renderSignals(signals) {
  document.getElementById('signal-count').textContent = `(${signals.length} signals)`;
  const tbody = document.getElementById('signals-tbody');
  if (!signals.length) {
    tbody.innerHTML = '<tr><td colspan="13" style="text-align:center;color:var(--muted);padding:24px;">暂无信号 — 等待扫描...</td></tr>';
    return;
  }
  tbody.innerHTML = signals.map(s => {
    const sigClass = s.signal === 'BUY YES' ? 'sig-buy' : 'sig-sell';
    const emoji = s.signal === 'BUY YES' ? '🟢' : '🔴';
    const pmUrl = s.url || (s.slug ? 'https://polymarket.com/market/' + s.slug : '');
    const link = pmUrl ? `<a href="${pmUrl}" target="_blank" style="color:var(--blue);text-decoration:none;" title="Open in Polymarket">🔗</a>` : '';
    return `<tr>
      <td>${s.ts ? s.ts.slice(11,19) : '—'}</td>
      <td><span class="badge ${sigClass}">${emoji} ${s.signal}</span></td>
      <td><span class="tier tier-${s.tier}">${s.tier}</span></td>
      <td>${s.city || '—'}</td>
      <td>${s.direction || ''} ${s.threshold || ''}${s.unit||''}</td>
      <td title="${s.question||''}">${(s.question||'').substring(0,55)}</td>
      <td>${fmtEdge(s.edge)}</td>
      <td>${s.confidence || 0}%</td>
      <td>${pct(s.market_prob)}</td>
      <td>${pct(s.model_prob)}</td>
      <td>${fmt(s.position_size)}</td>
      <td>${s.volume ? '$'+Math.round(s.volume).toLocaleString() : '—'}</td>
      <td>${link}</td>
    </tr>`;
  }).join('');
}

function renderPositions(positions) {
  document.getElementById('pos-count').textContent = `(${positions.length} open)`;
  const tbody = document.getElementById('pos-tbody');
  if (!positions.length) {
    tbody.innerHTML = '<tr><td colspan="10" style="text-align:center;color:var(--muted);padding:24px;">无持仓 — 等待A级信号触发</td></tr>';
    return;
  }
  tbody.innerHTML = positions.map(p => {
    const sigClass = p.signal === 'BUY YES' ? 'sig-buy' : 'sig-sell';
    const pmUrl = p.slug ? 'https://polymarket.com/market/' + p.slug : '';
    const marketCell = pmUrl
      ? `<a href="${pmUrl}" target="_blank" style="color:var(--blue);text-decoration:none;" title="Open in Polymarket">${(p.question||'').substring(0,55)}</a>`
      : (p.question||'').substring(0,55);
    const curPrice = p.current_price || p.entry_price || 0;
    const floatPnl = p.floating_pnl || 0;
    const isOpen = p.status === 'open';
    const priceCls = isOpen && curPrice > (p.entry_price||0) ? 'pnl-pos' : isOpen && curPrice < (p.entry_price||0) ? 'pnl-neg' : '';
    const floatCls = floatPnl >= 0 ? 'pnl-pos' : 'pnl-neg';
    return `<tr>
      <td>${p.opened_at ? p.opened_at.slice(0,16) : '—'}</td>
      <td>${p.city || '—'}</td>
      <td><span class="badge ${sigClass}">${p.signal}</span></td>
      <td><span class="tier tier-${p.tier}">${p.tier}</span></td>
      <td>$${(p.entry_price||0).toFixed(4)}</td>
      <td class="${priceCls}">$${curPrice.toFixed(4)}</td>
      <td class="${floatCls}">${fmtPnL(floatPnl)}</td>
      <td>${(p.shares||0).toLocaleString()}</td>
      <td>${fmt(p.cost)}</td>
      <td title="${p.question||''}">${marketCell}</td>
    </tr>`;
  }).join('');
}

function load() {
  Promise.all([
    fetch('/api/stats').then(r => r.json()),
    fetch('/api/signals').then(r => r.json()),
    fetch('/api/positions-live').then(r => r.json()),
    fetch('/api/resolved').then(r => r.json())
  ]).then(([stats, signals, positions, resolved]) => {
    renderCards({ stats, current_signals: signals.length });
    renderSignals(signals);
    renderPositions(positions);
    renderResolved(resolved);
    document.getElementById('refresh').textContent = 'Last refresh: ' + new Date().toLocaleTimeString();
    // Update status dot: green if any activity in last 10 min, else orange
    const lastTs = signals[0]?.ts;
    const stale = !lastTs || (Date.now() - new Date(lastTs).getTime()) > 600_000;
    document.getElementById('status-dot').className = stale ? 'dot dot-stale' : 'dot';
  }).catch(e => {
    document.getElementById('refresh').textContent = 'Error: ' + e.message;
    document.getElementById('status-dot').className = 'dot dot-stale';
  });
}

load();
setInterval(load, REFRESH * 1000);
</script>
</body>
</html>
""".replace("__REFRESH_SEC__", str(REFRESH_SEC))


# ── API Handlers ─────────────────────────────────────────────
def db_connect():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=2000")
    return conn


def get_stats() -> dict:
    if not DB_PATH.exists():
        return {"total_signals": 0, "open": 0, "resolved": 0, "realized_pnl": 0,
                "wins": 0, "losses": 0, "win_rate": 0,
                "initial_capital": 0, "deployed": 0, "available": 0}
    conn = db_connect()
    try:
        total = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        total_pos = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        open_n = conn.execute("SELECT COUNT(*) FROM positions WHERE status='open'").fetchone()[0]
        resolved = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(resolved_pnl),0) FROM positions WHERE status='resolved'"
        ).fetchone()
        wins = conn.execute(
            "SELECT COUNT(*) FROM positions WHERE status='resolved' AND resolved_pnl > 0"
        ).fetchone()[0]
        losses = conn.execute(
            "SELECT COUNT(*) FROM positions WHERE status='resolved' AND resolved_pnl < 0"
        ).fetchone()[0]
        # Balance from account table
        acc = conn.execute("SELECT initial_capital FROM account WHERE id=1").fetchone()
        initial = acc[0] if acc else 0
        deployed = conn.execute(
            "SELECT COALESCE(SUM(cost), 0) FROM positions WHERE status='open'"
        ).fetchone()[0]
        rpnl = round(resolved[1] or 0, 2)
        available = round(initial - deployed + rpnl, 2)
        return {
            "total_signals": total,
            "total_positions": total_pos,
            "open": open_n,
            "resolved": resolved[0] or 0,
            "realized_pnl": rpnl,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / max(resolved[0], 1) * 100, 1) if resolved[0] else 0,
            "initial_capital": round(initial, 2),
            "deployed": round(deployed, 2),
            "available": available,
        }
    finally:
        conn.close()


def get_signals(limit: int = 50) -> list:
    if not DB_PATH.exists():
        return []
    conn = db_connect()
    try:
        rows = conn.execute(
            "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        # Deduplicate by slug — keep latest per slug
        seen = set()
        result = []
        for r in rows:
            s = dict(r)
            if s["slug"] not in seen:
                seen.add(s["slug"])
                result.append(s)
                if len(result) >= 30:
                    break
        return result
    finally:
        conn.close()


def get_resolved() -> list:
    """Return resolved positions for display."""
    if not DB_PATH.exists():
        return []
    conn = db_connect()
    try:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status='resolved' ORDER BY resolved_at DESC LIMIT 50"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def get_positions() -> list:
    if not DB_PATH.exists():
        return []
    conn = db_connect()
    try:
        rows = conn.execute(
            "SELECT * FROM positions ORDER BY id DESC LIMIT 100"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_positions_live() -> list:
    """Enrich open positions with current market price from Gamma API."""
    positions = get_positions()
    open_positions = [p for p in positions if p.get("status") == "open"]
    if not open_positions:
        return []
    
    # Fetch prices one-by-one (Gamma API doesn't support comma-separated slugs)
    import subprocess, json
    prices = {}
    for p in open_positions:  # all positions
        slug = p["slug"]
        url = f"https://gamma-api.polymarket.com/markets?slug={slug}&limit=1"
        try:
            r = subprocess.run(
                ["/usr/bin/env", "-u", "SSL_CERT_FILE", "-u", "REQUESTS_CA_BUNDLE",
                 "/usr/bin/curl", "-s", "--connect-timeout", "5", "--max-time", "15", url],
                capture_output=True, text=True, timeout=18
            )
            if r.returncode == 0 and r.stdout.strip():
                markets = json.loads(r.stdout)
                if markets:
                    prices_str = markets[0].get("outcomePrices", "[]")
                    if isinstance(prices_str, str):
                        prices_str = json.loads(prices_str)
                    if prices_str and len(prices_str) >= 2:
                        prices[slug] = {
                            "yes": float(prices_str[0]) if prices_str[0] else 0,
                            "no": float(prices_str[1]) if prices_str[1] else 0,
                        }
        except Exception:
            pass
    
    for p in positions:
        if p.get("status") == "open":
            price_data = prices.get(p["slug"], {})
            shares = p.get("shares", 0)
            entry = p.get("entry_price", 0)
            signal = p.get("signal", "")
            
            # Direction-aware live price
            if isinstance(price_data, dict):
                cur_price = price_data.get("yes", 0) if signal == "BUY YES" else price_data.get("no", 0)
            else:
                cur_price = price_data
            
            if cur_price == 0:
                cur_price = entry
            
            if signal == "BUY YES":
                floating_pnl = (cur_price - entry) * shares
            elif signal == "BUY NO":
                floating_pnl = (cur_price - entry) * shares
            else:
                floating_pnl = 0
            
            p["current_price"] = round(cur_price, 4)
            p["floating_pnl"] = round(floating_pnl, 2)
        else:
            p["current_price"] = p.get("entry_price", 0)
            p["floating_pnl"] = p.get("resolved_pnl", 0)
    
    return [p for p in positions if p.get("status") == "open"]



def get_breakdown():
    if not DB_PATH.exists():
        return {}
    import sqlite3
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    result = {}
    for sig in ["BUY YES", "BUY NO"]:
        total = conn.execute("SELECT COUNT(*) FROM positions WHERE signal=? AND status='resolved'", (sig,)).fetchone()[0]
        wins = conn.execute("SELECT COUNT(*) FROM positions WHERE signal=? AND status='resolved' AND resolved_pnl>0", (sig,)).fetchone()[0]
        losses = conn.execute("SELECT COUNT(*) FROM positions WHERE signal=? AND status='resolved' AND resolved_pnl<0", (sig,)).fetchone()[0]
        pnl = conn.execute("SELECT COALESCE(SUM(resolved_pnl),0) FROM positions WHERE signal=? AND status='resolved'", (sig,)).fetchone()[0]
        result[sig] = {"total": total, "wins": wins, "losses": losses, "win_rate": round(wins / max(total, 1) * 100, 1), "pnl": round(pnl, 2)}
    conn.close()
    return result

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/stats":
            self._json(get_stats())
        elif self.path == "/api/signals":
            self._json(get_signals())
        elif self.path == "/api/positions":
            self._json(get_positions())
        elif self.path == "/api/positions-live":
            self._json(get_positions_live())
        elif self.path == "/api/resolved":
            self._json(get_resolved())
        elif self.path == "/api/breakdown":
            self._json(get_breakdown())
        elif self.path in ("/", "/index.html"):
            self._html(PAGE)
        else:
            self._html(PAGE)
    
    def _json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str).encode())
    
    def _html(self, content: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(content.encode())
    
    def log_message(self, *args):
        pass


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Weather Alpha Dashboard")
    p.add_argument("--port", type=int, default=PORT)
    p.add_argument("--db", type=str, default=str(DB_PATH))
    args = p.parse_args()
    
    # Set DB path — must modify the module-level variable directly
    DB_PATH = Path(args.db)
    
    # Detect version from DB filename
    version = "v3" if "v3" in str(DB_PATH) else ("v4" if "v4" in str(DB_PATH) else "??")
    import sys
    # Replace version placeholder in the HTML template
    mod = sys.modules[__name__]
    mod.PAGE = mod.PAGE.replace("__VERSION__", version)
    
    print(f"🌦 Weather Alpha {version} Dashboard")
    print(f"   http://localhost:{args.port}")
    print(f"   DB: {DB_PATH.resolve()}")
    print(f"   Refresh: {REFRESH_SEC}s")
    server = HTTPServer(("0.0.0.0", args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Done")



