#!/usr/bin/env python3
"""🐋 Whale Bot Dashboard — HTTP 看板"""

import sqlite3, json, sys
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

DB_PATH = Path(__file__).parent / "data" / "whale_bot.db"
PORT = 8771
REFRESH_SEC = 30

PAGE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🐋 Whale Bot Dashboard</title>
<style>
  :root {
    --bg: #0d1117; --card: #161b22; --border: #30363d;
    --text: #c9d1d9; --muted: #8b949e; --green: #3fb950;
    --red: #f85149; --blue: #58a6ff; --orange: #d2991d;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, sans-serif; padding: 20px; max-width: 1400px; margin: 0 auto; }
  h1 { font-size: 22px; margin-bottom: 16px; display: flex; align-items: center; gap: 8px; }
  h1 .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--green); display: inline-block; animation: pulse 2s infinite; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin-bottom: 20px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; }
  .card .label { font-size: 12px; color: var(--muted); text-transform: uppercase; margin-bottom: 4px; }
  .card .value { font-size: 26px; font-weight: 700; }
  .card .sub { font-size: 12px; color: var(--muted); margin-top: 2px; }
  .val-good { color: var(--green); } .val-bad { color: var(--red); } .val-neutral { color: var(--blue); }
  .controls { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
  .controls button, .controls select { background: var(--card); color: var(--text); border: 1px solid var(--border); padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 13px; }
  .controls button:hover { background: #21262d; }
  .controls button.active { background: var(--blue); color: #000; border-color: var(--blue); }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; padding: 10px 12px; border-bottom: 1px solid var(--border); color: var(--muted); font-weight: 500; font-size: 11px; text-transform: uppercase; }
  td { padding: 8px 12px; border-bottom: 1px solid var(--border); }
  tr:hover { background: #1c2128; }
  .pnl-pos { color: var(--green); font-weight: 600; }
  .pnl-neg { color: var(--red); font-weight: 600; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
  .badge-open { background: rgba(88,166,255,0.15); color: var(--blue); }
  .badge-win { background: rgba(63,185,80,0.15); color: var(--green); }
  .badge-loss { background: rgba(248,81,73,0.15); color: var(--red); }
  .badge-up { background: rgba(63,185,80,0.12); color: var(--green); }
  .badge-down { background: rgba(248,81,73,0.12); color: var(--red); }
  .refresh { font-size: 11px; color: var(--muted); text-align: right; margin-top: 8px; }
</style>
</head>
<body>
<h1><span class="dot"></span>🐋 Whale Bot — Top-Holder Signal Sim</h1>

<div class="cards" id="cards"></div>

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
  </select>
</div>

<table>
<thead><tr>
  <th>ID</th><th>时间</th><th>市场</th><th>方向</th><th>价格</th><th>份额</th><th>成本</th>
  <th>🐋UP3</th><th>🐋DN3</th><th>置信</th><th>状态</th><th>PnL</th>
</tr></thead>
<tbody id="tbody"></tbody>
</table>
<div class="refresh" id="refresh">...</div>

<script>
let allData = [];
const REFRESH = __REFRESH__;

function fmt(n) { return '$' + (n||0).toFixed(2); }
function fmtPnL(n) {
  if (n === null || n === undefined) return '—';
  return (n>=0?'+':'') + '$' + n.toFixed(2);
}
function fmtN(n) { return n ? Number(n).toLocaleString() : '0'; }

function renderCards() {
  let resolved=0, open=0, cost=0, pnl=0;
  allData.forEach(r => {
    if(r.status==='resolved') { resolved++; pnl+=r.pnl||0; }
    else { open++; cost+=r.cost||0; }
  });
  const total=allData.length;
  const avail=200-cost;
  document.getElementById('cards').innerHTML = `
    <div class="card"><div class="label">总交易</div><div class="value val-neutral">${total}</div></div>
    <div class="card"><div class="label">持仓中</div><div class="value val-neutral">${open}</div><div class="sub">成本 ${fmt(cost)}</div></div>
    <div class="card"><div class="label">已结算</div><div class="value val-neutral">${resolved}</div></div>
    <div class="card"><div class="label">已实现 PnL</div><div class="value ${pnl>=0?'val-good':'val-bad'}">${fmtPnL(pnl)}</div></div>
    <div class="card"><div class="label">可用余额</div><div class="value val-neutral">${fmt(avail)}</div></div>
    <div class="card"><div class="label">总资产</div><div class="value ${avail+pnl>=200?'val-good':'val-bad'}">${fmt(avail+pnl)}</div></div>
  `;
}

function renderTable(data) {
  document.getElementById('tbody').innerHTML = data.map(r => {
    const sb = r.status==='open'
      ? '<span class="badge badge-open">持仓中</span>'
      : (r.pnl>0?'<span class="badge badge-win">已结算</span>':'<span class="badge badge-loss">已结算</span>');
    const dir = r.side==='Up'?'<span class="badge badge-up">UP</span>':'<span class="badge badge-down">DN</span>';
    const p = r.pnl!==null?fmtPnL(r.pnl):'—';
    const pc = r.pnl>0?'pnl-pos':(r.pnl<0?'pnl-neg':'');
    return `<tr>
      <td>${r.id}</td><td>${(r.time||'').slice(5,19)}</td>
      <td title="${r.title}">${(r.title||'').slice(0,50)}</td>
      <td>${dir}</td><td>$${(r.price||0).toFixed(4)}</td>
      <td>${r.shares||0}</td><td>$${(r.cost||0).toFixed(2)}</td>
      <td>${fmtN(r.up_top3)}</td><td>${fmtN(r.down_top3)}</td>
      <td>${((r.whale_conf||0)*100).toFixed(0)}%</td>
      <td>${sb}</td><td class="${pc}">${p}</td>
    </tr>`;
  }).join('');
  document.getElementById('refresh').textContent = 'Last refresh: ' + new Date().toLocaleTimeString();
}

function load() {
  fetch('/api/trades').then(r=>r.json()).then(d=>{ allData=d; renderCards(); renderTable(d); });
}
let curF='all', curS='time_desc';
function filterTable(f,b) {
  curF=f;
  document.querySelectorAll('.controls button').forEach(x=>x.classList.remove('active'));
  if(b) b.classList.add('active');
  let d=allData;
  if(f==='open') d=allData.filter(r=>r.status==='open');
  else if(f==='resolved') d=allData.filter(r=>r.status==='resolved');
  else if(f==='win') d=allData.filter(r=>r.pnl>0);
  else if(f==='loss') d=allData.filter(r=>r.pnl<0);
  sortAndRender(d);
}
function sortTable(s) {
  curS=s;
  let d=allData;
  if(curF==='open') d=allData.filter(r=>r.status==='open');
  else if(curF==='resolved') d=allData.filter(r=>r.status==='resolved');
  else if(curF==='win') d=allData.filter(r=>r.pnl>0);
  else if(curF==='loss') d=allData.filter(r=>r.pnl<0);
  sortAndRender(d);
}
function sortAndRender(d) {
  const a=[...d];
  if(curS==='time_desc') a.sort((x,y)=>y.id-x.id);
  else if(curS==='time_asc') a.sort((x,y)=>x.id-y.id);
  else if(curS==='pnl_desc') a.sort((x,y)=>(y.pnl||0)-(x.pnl||0));
  else if(curS==='pnl_asc') a.sort((x,y)=>(x.pnl||0)-(y.pnl||0));
  renderTable(a);
}
load();
setInterval(load, REFRESH*1000);
</script>
</body>
</html>""".replace("__REFRESH__", str(REFRESH_SEC))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/trades":
            self._json(get_trades())
        elif self.path == "/api/stats":
            self._json(get_stats())
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(PAGE.encode())

    def _json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, *a): pass


def get_trades():
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, bet_at as time, market_slug, market_title as title,
               side, price, shares, cost,
               whale_dir, whale_conf, up_top3, down_top3,
               status,
               CASE WHEN status='resolved' THEN resolved_pnl ELSE NULL END as pnl
        FROM trades ORDER BY id DESC LIMIT 500
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stats():
    if not DB_PATH.exists():
        return {"total": 0, "open": 0, "resolved": 0}
    conn = sqlite3.connect(str(DB_PATH))
    total = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    open_n = conn.execute("SELECT COUNT(*) FROM trades WHERE status='open'").fetchone()[0]
    r = conn.execute("SELECT COUNT(*), COALESCE(SUM(resolved_pnl),0) FROM trades WHERE status='resolved'").fetchone()
    cost = conn.execute("SELECT COALESCE(SUM(cost),0) FROM trades WHERE status='open'").fetchone()[0]
    conn.close()
    avail = max(0, 200 - cost)
    return {
        "total": total, "open": open_n, "resolved": r[0],
        "realized_pnl": round(r[1], 2), "open_cost": round(cost, 2),
        "avail": round(avail, 2), "total_assets": round(avail + r[1], 2),
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()
    print(f"🐋 Whale Bot Dashboard — http://localhost:{args.port}")
    HTTPServer(("0.0.0.0", args.port), Handler).serve_forever()
