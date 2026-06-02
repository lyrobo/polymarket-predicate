#!/usr/bin/env python3
"""
🌤 WeatherHK Sim Dashboard — HTTP 看板

Shows all simulated copy trades with PnL tracking.
Auto-refreshes every 30s.

Usage: python3 weather_dashboard.py [--port 8767]
"""

import sqlite3, json, time, sys
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

DB_PATH = Path(__file__).parent / "data" / "weather_sim.db"
PORT = 8767
REFRESH_SEC = 30

CLAUDE_JSON = Path(__file__).parent / "data" / "claude_signal.json"

PAGE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🌤 Weather Sim Dashboard</title>
<style>
  :root {
    --bg: #0d1117;
    --card: #161b22;
    --border: #30363d;
    --text: #c9d1d9;
    --muted: #8b949e;
    --green: #3fb950;
    --red: #f85149;
    --blue: #58a6ff;
    --orange: #d2991d;
    --purple: #a371f7;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { 
    background: var(--bg); color: var(--text); 
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    padding: 20px; max-width: 1400px; margin: 0 auto;
  }
  h1 { font-size: 22px; margin-bottom: 16px; display: flex; align-items: center; gap: 8px; }
  h1 .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--green); display: inline-block; animation: pulse 2s infinite; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }

  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 20px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; }
  .card .label { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; }
  .card .value { font-size: 26px; font-weight: 700; }
  .card .sub { font-size: 12px; color: var(--muted); margin-top: 2px; }
  .val-good { color: var(--green); }
  .val-bad { color: var(--red); }
  .val-neutral { color: var(--blue); }

  .controls { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
  .controls button, .controls select {
    background: var(--card); color: var(--text); border: 1px solid var(--border);
    padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 13px;
  }
  .controls button:hover { background: #21262d; }
  .controls button.active { background: var(--blue); color: #000; border-color: var(--blue); }

  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; padding: 10px 12px; border-bottom: 1px solid var(--border); color: var(--muted); font-weight: 500; font-size: 11px; text-transform: uppercase; position: sticky; top:0; background: var(--bg); z-index: 1; }
  td { padding: 8px 12px; border-bottom: 1px solid var(--border); }
  tr:hover { background: #1c2128; }
  .tr-open {}
  .tr-resolved { opacity: 0.7; }
  .pnl-pos { color: var(--green); font-weight: 600; }
  .pnl-neg { color: var(--red); font-weight: 600; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
  .badge-open { background: rgba(88,166,255,0.15); color: var(--blue); }
  .badge-win { background: rgba(63,185,80,0.15); color: var(--green); }
  .badge-loss { background: rgba(248,81,73,0.15); color: var(--red); }
  .badge-buy { background: rgba(63,185,80,0.12); color: var(--green); }
  .badge-sell { background: rgba(248,81,73,0.12); color: var(--red); }

  .refresh { font-size: 11px; color: var(--muted); text-align: right; margin-top: 8px; }
</style>
</head>
<body>
<h1><span class="dot"></span>🌤 WeatherHK Sim Dashboard — $700 Virtual</h1>

<div class="cards" id="cards"></div>

<h1 style="margin-top: 24px;">🤖 Claude Bot Signal — <span style="font-size:14px;color:var(--muted)">0xb55fa...1764d4</span></h1>
<div class="cards" id="claude-cards"></div>
<div id="claude-accumulations" style="margin-bottom:16px;"></div>

<div class="controls">
  <button class="active" onclick="filterTable('all', this)">全部</button>
  <button onclick="filterTable('open', this)">持仓中</button>
  <button onclick="filterTable('resolved', this)">已结算</button>
  <button onclick="filterTable('win', this)">盈利</button>
  <button onclick="filterTable('loss', this)">亏损</button>
  <select onchange="sortTable(this.value)">
    <option value="time_desc">时间↓</option>
    <option value="time_asc">时间↑</option>
    <option value="pnl_desc">PnL↓</option>
    <option value="pnl_asc">PnL↑</option>
    <option value="cost_desc">金额↓</option>
  </select>
</div>

<table>
<thead>
<tr>
  <th>ID</th><th>时间</th><th>市场</th><th>方向</th><th>价格</th><th>份额</th><th>成本</th><th>状态</th><th>PnL</th>
</tr>
</thead>
<tbody id="tbody"></tbody>
</table>

<div class="refresh" id="refresh">Last refresh: ...</div>

<script>
let allData = [];
const REFRESH = __REFRESH_SEC__;

function fmt(n) { 
  if (typeof n !== 'number') return n;
  return '$' + n.toFixed(2); 
}
function fmtPnL(n) {
  if (typeof n !== 'number' || isNaN(n)) return '—';
  const sign = n >= 0 ? '+' : '';
  return sign + '$' + n.toFixed(2);
}

function renderCards() {
  let resolved = 0, open = 0, totalCost = 0, totalPnL = 0;
  allData.forEach(r => {
    if (r.status === 'resolved') { resolved++; totalPnL += r.pnl; }
    else { open++; totalCost += r.cost; }
  });
  const avail = 700 - totalCost;
  const total = allData.length;
  document.getElementById('cards').innerHTML = `
    <div class="card"><div class="label">总交易</div><div class="value val-neutral">${total}</div></div>
    <div class="card"><div class="label">持仓中</div><div class="value val-neutral">${open}</div><div class="sub">成本 ${fmt(totalCost)}</div></div>
    <div class="card"><div class="label">已结算</div><div class="value val-neutral">${resolved}</div></div>
    <div class="card"><div class="label">已实现 PnL</div><div class="value ${totalPnL>=0?'val-good':'val-bad'}">${fmtPnL(totalPnL)}</div></div>
    <div class="card"><div class="label">可用余额</div><div class="value val-neutral">${fmt(avail)}</div></div>
    <div class="card"><div class="label">总资产</div><div class="value ${(avail+totalPnL)>=700?'val-good':'val-bad'}">${fmt(avail + totalPnL)}</div></div>
  `;
}

function renderTable(data) {
  const tbody = document.getElementById('tbody');
  tbody.innerHTML = data.map(r => {
    const statusBadge = r.status === 'open'
      ? '<span class="badge badge-open">持仓中</span>'
      : (r.pnl > 0 ? '<span class="badge badge-win">已结算</span>' : '<span class="badge badge-loss">已结算</span>');
    const sideBadge = r.side === 'BUY' ? '<span class="badge badge-buy">BUY</span>' : '<span class="badge badge-sell">SELL</span>';
    const pnlClass = r.pnl > 0 ? 'pnl-pos' : (r.pnl < 0 ? 'pnl-neg' : '');
    return `<tr class="tr-${r.status}">
      <td>${r.id}</td>
      <td>${r.time}</td>
      <td title="${r.title}">${r.title}</td>
      <td>${sideBadge}</td>
      <td>$${r.price.toFixed(4)}</td>
      <td>${r.shares}</td>
      <td>$${r.cost.toFixed(2)}</td>
      <td>${statusBadge}</td>
      <td class="${pnlClass}">${r.pnl !== null ? fmtPnL(r.pnl) : '—'}</td>
    </tr>`;
  }).join('');
  document.getElementById('refresh').textContent = 'Last refresh: ' + new Date().toLocaleTimeString();
}

function load() {
  fetch('/api/trades').then(r => r.json()).then(data => {
    allData = data;
    renderCards();
    renderTable(data);
  });
}

let currentFilter = 'all';
let currentSort = 'time_desc';
function filterTable(f, btn) {
  currentFilter = f;
  document.querySelectorAll('.controls button').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  let filtered = allData;
  if (f === 'open') filtered = allData.filter(r => r.status === 'open');
  else if (f === 'resolved') filtered = allData.filter(r => r.status === 'resolved');
  else if (f === 'win') filtered = allData.filter(r => r.pnl > 0);
  else if (f === 'loss') filtered = allData.filter(r => r.pnl < 0);
  sortAndRender(filtered);
}
function sortTable(s) {
  currentSort = s;
  let data = allData;
  if (currentFilter === 'open') data = allData.filter(r => r.status === 'open');
  else if (currentFilter === 'resolved') data = allData.filter(r => r.status === 'resolved');
  else if (currentFilter === 'win') data = allData.filter(r => r.pnl > 0);
  else if (currentFilter === 'loss') data = allData.filter(r => r.pnl < 0);
  sortAndRender(data);
}
function sortAndRender(data) {
  const d = [...data];
  if (currentSort === 'time_desc') d.sort((a,b) => b.id - a.id);
  else if (currentSort === 'time_asc') d.sort((a,b) => a.id - b.id);
  else if (currentSort === 'pnl_desc') d.sort((a,b) => (b.pnl||0) - (a.pnl||0));
  else if (currentSort === 'pnl_asc') d.sort((a,b) => (a.pnl||0) - (b.pnl||0));
  else if (currentSort === 'cost_desc') d.sort((a,b) => b.cost - a.cost);
  renderTable(d);
}
load();
setInterval(load, REFRESH * 1000);

// ── Claude Bot Signal ──
const SIG_COLORS = {
  STRONG_DOWN: 'val-bad', DOWN_BIAS: 'val-bad', NEUTRAL: 'val-neutral',
  UP_BIAS: 'val-good', STRONG_UP: 'val-good', NO_DATA: 'val-neutral'
};
const SIG_EMOJI = {
  STRONG_DOWN: '🔴', DOWN_BIAS: '🟠', NEUTRAL: '⚪',
  UP_BIAS: '🟢', STRONG_UP: '🟢', NO_DATA: '⚫'
};

function loadClaude() {
  fetch('/api/claude').then(r => r.json()).then(d => {
    const sig = d.signal || 'NO_DATA';
    const bias = d.bias || {};
    const zones = d.price_zones || {};
    const accs = d.accumulations || [];
    
    let zoneHtml = '';
    for (const [k, z] of Object.entries(zones)) {
      zoneHtml += `<span style="margin-right:12px;font-size:12px">
        ${k}: <b>${z.count}</b> trades @ avg <b>$${z.avg.toFixed(2)}</b> (${z.pct}%)</span>`;
    }
    
    document.getElementById('claude-cards').innerHTML = `
      <div class="card">
        <div class="label">信号</div>
        <div class="value ${SIG_COLORS[sig]||'val-neutral'}">${SIG_EMOJI[sig]||''} ${sig}</div>
        ${d.direction ? `<div class="sub">方向: ${d.direction}</div>` : ''}
      </div>
      <div class="card">
        <div class="label">Up 占比</div>
        <div class="value val-good">${bias.up_pct||0}%</div>
        <div class="sub">${bias.total_trades||0} 笔交易</div>
      </div>
      <div class="card">
        <div class="label">Down 占比</div>
        <div class="value val-bad">${bias.down_pct||0}%</div>
        <div class="sub">置信度 ${(d.strength*100||0).toFixed(0)}%</div>
      </div>
      <div class="card" style="grid-column: span 2;">
        <div class="label">入场价格区间</div>
        <div class="value" style="font-size:14px;">${zoneHtml || '—'}</div>
      </div>
    `;
    
    // Accumulations
    if (accs.length > 0) {
      document.getElementById('claude-accumulations').innerHTML = `
        <div style="background:var(--card);border:1px solid var(--orange);border-radius:8px;padding:12px 16px;">
          <div style="color:var(--orange);font-weight:600;margin-bottom:8px;">⚠️ 大仓积累 (${accs.length} 个)</div>
          ${accs.map(a => `<div style="font-size:12px;margin:4px 0;color:var(--text)">
            <b>${a.outcome}</b> ${a.title?.substring(0,50)}<br>
            <span style="color:var(--muted)">${a.shares.toLocaleString()} 股 @ $${a.avg_price.toFixed(3)} = $${a.cost.toLocaleString()}</span>
          </div>`).join('')}
        </div>`;
    } else {
      document.getElementById('claude-accumulations').innerHTML = '';
    }
  }).catch(() => {
    document.getElementById('claude-cards').innerHTML = '<div class="card"><div class="label">Claude Bot</div><div class="value val-neutral">等待数据...</div></div>';
  });
}
loadClaude();
setInterval(loadClaude, REFRESH * 1000);
</script>
</body>
</html>""".replace("__REFRESH_SEC__", str(REFRESH_SEC))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/trades":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            trades = get_trades()
            self.wfile.write(json.dumps(trades).encode())
        elif self.path == "/api/stats":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            stats = get_stats()
            self.wfile.write(json.dumps(stats).encode())
        elif self.path == "/api/claude":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            claude = get_claude_signal()
            self.wfile.write(json.dumps(claude).encode())
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(PAGE.encode())

    def log_message(self, format, *args):
        # Suppress default logging
        pass


def get_claude_signal():
    """Read latest Claude Bot signal from JSON file."""
    if not CLAUDE_JSON.exists():
        return {"signal": "NO_DATA", "error": "No signal data yet"}
    try:
        with open(CLAUDE_JSON) as f:
            return json.load(f)
    except Exception:
        return {"signal": "ERROR", "error": "Failed to read signal file"}


def get_trades():
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, copied_at as time, market_title as title,
                  our_side as side, our_price as price, our_shares as shares,
                  our_cost as cost, status,
                  CASE WHEN status='resolved' THEN resolved_pnl ELSE NULL END as pnl
           FROM sim_trades
           ORDER BY id DESC
           LIMIT 500"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stats():
    if not DB_PATH.exists():
        return {"total": 0, "open": 0, "resolved": 0, "pnl": 0}
    conn = sqlite3.connect(str(DB_PATH))
    total = conn.execute("SELECT COUNT(*) FROM sim_trades").fetchone()[0]
    open_n = conn.execute("SELECT COUNT(*) FROM sim_trades WHERE status='open'").fetchone()[0]
    resolved = conn.execute("SELECT COUNT(*), COALESCE(SUM(resolved_pnl),0) FROM sim_trades WHERE status='resolved'").fetchone()
    cost_open = conn.execute("SELECT COALESCE(SUM(our_cost),0) FROM sim_trades WHERE status='open'").fetchone()[0]
    conn.close()
    avail = max(0, 700 - cost_open)
    return {
        "total": total,
        "open": open_n,
        "resolved": resolved[0],
        "realized_pnl": round(resolved[1], 2),
        "open_cost": round(cost_open, 2),
        "avail": round(avail, 2),
        "total_assets": round(avail + resolved[1], 2),
    }


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=PORT)
    args = p.parse_args()

    print(f"🌤 WeatherHK Sim Dashboard")
    print(f"   http://localhost:{args.port}")
    print(f"   Refresh: {REFRESH_SEC}s")
    print(f"   DB: {DB_PATH}")
    server = HTTPServer(("0.0.0.0", args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Done")
