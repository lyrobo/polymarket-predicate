"""
🤖 Dolita Sim Dashboard — HTTP Server on port 8768

Shows virtual portfolio tracking dolita's BTC Up/Down copy trades.
"""

import http.server
import json
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "dolita_sim.db"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8768


def get_stats():
    """Read stats from sim database."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    
    # Overall stats
    total = conn.execute("SELECT COUNT(*) as cnt FROM sim_trades").fetchone()['cnt']
    open_count = conn.execute("SELECT COUNT(*) as cnt FROM sim_trades WHERE status='open'").fetchone()['cnt']
    resolved = conn.execute(
        "SELECT COUNT(*) as cnt, COALESCE(SUM(resolved_pnl),0) as pnl FROM sim_trades WHERE status='resolved'"
    ).fetchone()
    open_cost = conn.execute(
        "SELECT COALESCE(SUM(our_cost),0) as cost FROM sim_trades WHERE status='open'"
    ).fetchone()['cost']
    
    resolved_wins = conn.execute(
        "SELECT COUNT(*) as cnt FROM sim_trades WHERE status='resolved' AND resolved_pnl > 0"
    ).fetchone()['cnt']
    resolved_losses = conn.execute(
        "SELECT COUNT(*) as cnt FROM sim_trades WHERE status='resolved' AND resolved_pnl <= 0"
    ).fetchone()['cnt']
    
    # Open positions
    open_positions = conn.execute(
        "SELECT * FROM sim_trades WHERE status='open' ORDER BY copied_at DESC LIMIT 20"
    ).fetchall()
    
    # Recent resolved
    recent = conn.execute(
        "SELECT * FROM sim_trades WHERE status='resolved' ORDER BY resolved_at DESC LIMIT 20"
    ).fetchall()
    
    # Summary by day
    daily = conn.execute("""
        SELECT substr(copied_at, 1, 10) as day,
               COUNT(*) as trades,
               COUNT(CASE WHEN status='resolved' AND resolved_pnl > 0 THEN 1 END) as wins,
               COUNT(CASE WHEN status='resolved' AND resolved_pnl <= 0 THEN 1 END) as losses,
               COUNT(CASE WHEN status='open' THEN 1 END) as open,
               COALESCE(SUM(CASE WHEN status='resolved' THEN resolved_pnl ELSE 0 END), 0) as pnl,
               COALESCE(SUM(our_cost), 0) as invested
        FROM sim_trades
        GROUP BY day
        ORDER BY day DESC
        LIMIT 10
    """).fetchall()
    
    conn.close()
    
    return {
        "total": total,
        "open": open_count,
        "resolved": resolved['cnt'],
        "resolved_wins": resolved_wins,
        "resolved_losses": resolved_losses,
        "realized_pnl": round(resolved['pnl'], 2),
        "open_cost": round(open_cost, 2),
        "invested_total": round(open_cost + sum(abs(float(r['our_cost'])) for r in recent), 2),
        "open_positions": [dict(r) for r in open_positions],
        "recent_resolved": [dict(r) for r in recent],
        "daily": [dict(r) for r in daily],
    }


HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="15">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🤖 Dolita Sim 跟单看板</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'Segoe UI', system-ui, sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; }
h1 { font-size: 22px; margin-bottom: 16px; color: #58a6ff; }
h2 { font-size: 16px; color: #8b949e; margin: 20px 0 8px; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-bottom: 20px; }
.card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px; text-align: center; }
.card .label { font-size: 11px; color: #8b949e; text-transform: uppercase; margin-bottom: 4px; }
.card .value { font-size: 24px; font-weight: 700; }
.card .sub { font-size: 11px; color: #8b949e; margin-top: 2px; }
.green { color: #3fb950; }
.red { color: #f85149; }
.yellow { color: #d2991d; }
.blue { color: #58a6ff; }
table { width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 16px; }
th { text-align: left; padding: 8px; background: #161b22; border-bottom: 2px solid #30363d; color: #8b949e; font-weight: 600; position: sticky; top: 0; }
td { padding: 7px 8px; border-bottom: 1px solid #21262d; }
tr:hover { background: #1c2128; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
.badge-win { background: rgba(63,185,80,0.15); color: #3fb950; }
.badge-loss { background: rgba(248,81,73,0.15); color: #f85149; }
.badge-open { background: rgba(88,166,255,0.15); color: #58a6ff; }
.mono { font-family: 'SF Mono', 'Cascadia Code', monospace; font-size: 12px; }
.footer { color: #484f58; font-size: 11px; text-align: center; margin-top: 30px; }
</style>
</head>
<body>
<h1>🤖 Dolita 模拟跟单看板</h1>
<div class="stats">
  <div class="card">
    <div class="label">总交易</div>
    <div class="value blue">{total}</div>
  </div>
  <div class="card">
    <div class="label">持仓中</div>
    <div class="value yellow">{open}</div>
    <div class="sub">成本 __open_cost__</div>
  </div>
  <div class="card">
    <div class="label">已结算</div>
    <div class="value">{resolved}</div>
    <div class="sub">✅{resolved_wins} / ❌{resolved_losses}</div>
  </div>
  <div class="card">
    <div class="label">已实现盈亏</div>
    <div class="value __pnl_class__">__realized_pnl__</div>
  </div>
</div>

<h2>📊 每日汇总</h2>
<table>
<tr><th>日期</th><th>交易</th><th>胜</th><th>负</th><th>持仓</th><th>盈亏</th><th>投入</th></tr>
{daily_rows}
</table>

<h2>📋 当前持仓</h2>
<table>
<tr><th>时间</th><th>市场</th><th>方向</th><th>买入价</th><th>股数</th><th>成本</th></tr>
{open_rows}
</table>

<h2>📜 最近结算</h2>
<table>
<tr><th>结算时间</th><th>市场</th><th>方向</th><th>买入价</th><th>股数</th><th>成本</th><th>盈亏</th></tr>
{resolved_rows}
</table>

<div class="footer">🤖 Dolita Sim Copier | 每15秒自动刷新 | {refresh_time}</div>
</body>
</html>"""


def render():
    data = get_stats()
    from datetime import datetime
    
    pnl_class = "green" if data['realized_pnl'] >= 0 else "red"
    
    daily_rows = ""
    for d in data['daily']:
        pnl_c = "green" if d['pnl'] >= 0 else "red"
        daily_rows += (
            f"<tr><td>{d['day']}</td><td>{d['trades']}</td>"
            f"<td>{d['wins'] or '-'}</td><td>{d['losses'] or '-'}</td>"
            f"<td>{d['open'] or '-'}</td>"
            f"<td class='{pnl_c}'>${d['pnl']:+,.2f}</td>"
            f"<td>${d['invested']:,.2f}</td></tr>"
        )
    
    open_rows = ""
    for op in data['open_positions']:
        ts = op.get('copied_at', '')[:16].replace('T', ' ')
        open_rows += (
            f"<tr><td class='mono'>{ts}</td>"
            f"<td>{op.get('market_title', '')[:50]}</td>"
            f"<td>{op.get('our_side', '')}</td>"
            f"<td class='mono'>${op.get('our_price', 0):.4f}</td>"
            f"<td>{op.get('our_shares', 0):,}</td>"
            f"<td>${op.get('our_cost', 0):,.2f}</td></tr>"
        )
    
    resolved_rows = ""
    for r in data['recent_resolved']:
        ts = r.get('resolved_at', '')[:16].replace('T', ' ')
        pnl = r.get('resolved_pnl', 0)
        pnl_c = "green" if pnl >= 0 else "red"
        badge = '<span class="badge badge-win">WIN</span>' if pnl > 0 else '<span class="badge badge-loss">LOSS</span>'
        resolved_rows += (
            f"<tr><td class='mono'>{ts}</td>"
            f"<td>{r.get('market_title', '')[:40]}</td>"
            f"<td>{r.get('our_side', '')}</td>"
            f"<td class='mono'>${r.get('our_price', 0):.4f}</td>"
            f"<td>{r.get('our_shares', 0):,}</td>"
            f"<td>${r.get('our_cost', 0):,.2f}</td>"
            f"<td class='{pnl_c}'>{badge} ${pnl:+,.2f}</td></tr>"
        )
    
    html = HTML
    html = html.replace("{total}", str(data['total']))
    html = html.replace("{open}", str(data['open']))
    html = html.replace("{resolved}", str(data['resolved']))
    html = html.replace("{resolved_wins}", str(data['resolved_wins']))
    html = html.replace("{resolved_losses}", str(data['resolved_losses']))
    html = html.replace("__open_cost__", f"${data['open_cost']:,.2f}")
    html = html.replace("__realized_pnl__", f"${data['realized_pnl']:+,.2f}")
    html = html.replace("__pnl_class__", pnl_class)
    html = html.replace("{daily_rows}", daily_rows or "<tr><td colspan='7' style='color:#8b949e'>暂无数据</td></tr>")
    html = html.replace("{open_rows}", open_rows or "<tr><td colspan='6' style='color:#8b949e'>暂无持仓</td></tr>")
    html = html.replace("{resolved_rows}", resolved_rows or "<tr><td colspan='7' style='color:#8b949e'>暂无结算</td></tr>")
    html = html.replace("{refresh_time}", datetime.now().strftime('%H:%M:%S'))
    return html


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/stats":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(get_stats(), default=str).encode())
        else:
            html = render()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())


def main():
    server = http.server.HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"🤖 Dolita Sim Dashboard: http://localhost:{PORT}")
    print(f"   DB: {DB_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
