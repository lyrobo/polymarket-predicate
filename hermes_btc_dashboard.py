"""
🎯 Hermes BTC 5M Dashboard — HTTP Server on port 8769
"""
import http.server
import json
import sqlite3
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta
import subprocess

BASE_DIR = Path(__file__).parent

# Overridden by CLI args
DB_PATH = BASE_DIR / "data" / "hermes_btc_sim.db"
PORT = 8769
HKT = timezone(timedelta(hours=8))


def curl(url):
    try:
        r = subprocess.run(
            ["/usr/bin/env", "-u", "SSL_CERT_FILE", "-u", "REQUESTS_CA_BUNDLE",
             "/usr/bin/curl", "-s", "--connect-timeout", "3", "--max-time", "8", url],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except:
        pass
    return None


def get_btc_price():
    data = curl("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
    if data and "price" in data:
        return float(data["price"])
    return None


def get_stats():
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row

    # Markets
    markets = conn.execute(
        "SELECT slug, title, et_start, et_end, total_up_cost, total_down_cost, "
        "up_shares, down_shares, resolved, result, payout, pnl "
        "FROM markets ORDER BY et_start DESC LIMIT 30"
    ).fetchall()

    # Trades
    trades = conn.execute(
        "SELECT timestamp, market_slug, side, price, shares, cost, btc_price, up_weight "
        "FROM trades ORDER BY id DESC LIMIT 50"
    ).fetchall()

    # Aggregate from trades (since markets table may not be updated)
    total_trades = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    total_spent = conn.execute("SELECT COALESCE(SUM(cost),0) FROM trades").fetchone()[0]

    # Get max balance from snapshots
    snap = conn.execute(
        "SELECT balance, total_pnl FROM snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    balance = snap["balance"] if snap else 700.0
    total_pnl = snap["total_pnl"] if snap else 0.0

    # Per-market summary from trades
    mkt_rows = conn.execute(
        "SELECT market_slug, COUNT(*) as cnt, SUM(cost) as spent, "
        "SUM(CASE WHEN side='Up' THEN cost ELSE 0 END) as up_spent, "
        "SUM(CASE WHEN side='Down' THEN cost ELSE 0 END) as down_spent "
        "FROM trades GROUP BY market_slug ORDER BY market_slug DESC"
    ).fetchall()

    # Resolved markets
    resolved = conn.execute(
        "SELECT slug, title, pnl, result FROM markets WHERE resolved=1 ORDER BY et_start DESC LIMIT 20"
    ).fetchall()

    conn.close()

    return {
        "balance": balance,
        "total_pnl": total_pnl,
        "total_trades": total_trades,
        "total_spent": total_spent,
        "markets": [dict(m) for m in markets],
        "trades": [dict(t) for t in trades],
        "mkt_summary": [dict(m) for m in mkt_rows],
        "resolved": [dict(r) for r in resolved],
    }


HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="15">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hermes BTC 5M Dashboard</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { background: #0d1117; color: #c9d1d9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; padding: 20px; max-width: 1100px; margin: 0 auto; }
h1 { font-size: 22px; margin-bottom: 16px; }
h2 { font-size: 16px; margin: 20px 0 10px; color: #8b949e; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-bottom: 16px; }
.card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px; text-align: center; }
.card .label { font-size: 11px; color: #8b949e; text-transform: uppercase; margin-bottom: 4px; }
.card .value { font-size: 24px; font-weight: 700; }
.card .sub { font-size: 12px; color: #8b949e; margin-top: 4px; }
.green { color: #3fb950; }
.red { color: #f85149; }
.yellow { color: #d2991d; }
.blue { color: #58a6ff; }
table { width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 16px; }
th { text-align: left; padding: 8px; background: #161b22; border-bottom: 2px solid #30363d; color: #8b949e; font-weight: 600; }
td { padding: 7px 8px; border-bottom: 1px solid #21262d; }
tr:hover { background: #1c2128; }
.mono { font-family: 'SF Mono', 'Cascadia Code', monospace; font-size: 12px; }
.footer { color: #484f58; font-size: 11px; text-align: center; margin-top: 30px; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
.badge-win { background: rgba(63,185,80,0.15); color: #3fb950; }
.badge-loss { background: rgba(248,81,73,0.15); color: #f85149; }
.badge-up { background: rgba(63,185,80,0.15); color: #3fb950; }
.badge-down { background: rgba(248,81,73,0.15); color: #f85149; }
.momentum-bar { height: 6px; border-radius: 3px; background: #21262d; margin-top: 8px; overflow: hidden; }
.momentum-bar .fill { height: 100%; border-radius: 3px; transition: width 1s; }
.momentum-up { background: #3fb950; }
.momentum-down { background: #f85149; }
</style>
</head>
<body>
<h1>🎯 Hermes BTC 5M — 实盘交易看板</h1>

<div class="stats">
  <div class="card">
    <div class="label">💰 余额</div>
    <div class="value blue">{{balance}}</div>
  </div>
  <div class="card">
    <div class="label">📈 累计盈亏</div>
    <div class="value {{pnl_class}}">{{total_pnl}}</div>
  </div>
  <div class="card">
    <div class="label">📊 总交易</div>
    <div class="value">{{total_trades}}</div>
    <div class="sub">投入 {{total_spent}}</div>
  </div>
  <div class="card">
    <div class="label">₿ BTC 价格</div>
    <div class="value">{{btc_price}}</div>
    <div class="sub">{{btc_change}}</div>
  </div>
</div>

<h2>📋 交易记录</h2>
<table>
<tr><th>时间</th><th>市场 (ET)</th><th>方向</th><th>价格</th><th>股数</th><th>成本</th><th>BTC价</th><th>权重</th></tr>
{{trade_rows}}
</table>

<h2>🏷️ 市场汇总</h2>
<table>
<tr><th>市场</th><th>笔数</th><th>Up投入</th><th>Down投入</th><th>总计</th><th>结果</th><th>盈亏</th></tr>
{{mkt_rows}}
</table>

<h2>✅ 已结算</h2>
<table>
<tr><th>市场</th><th>结果</th><th>盈亏</th></tr>
{{resolved_rows}}
</table>

<div class="footer">🎯 Hermes BTC 5M Bot | 每15秒自动刷新 | {{refresh_time}}</div>
</body>
</html>"""


def render():
    data = get_stats()
    btc = get_btc_price()
    now = datetime.now(HKT)

    pnl_class = "green" if data["total_pnl"] >= 0 else "red"
    btc_str = f"${btc:,.0f}" if btc else "—"
    btc_change = ""

    # Trade rows
    trade_rows = ""
    for t in data["trades"][:30]:
        ts = t["timestamp"][11:19] if t["timestamp"] else ""
        slug_short = t["market_slug"][-12:] if t["market_slug"] else ""
        side = t["side"]
        side_cls = "badge-up" if side == "Up" else "badge-down"
        btc_p = f"${t.get('btc_price', 0):,.0f}" if t.get("btc_price") else "—"
        up_w = t.get("up_weight", 0.5)
        if up_w:
            w_bar = f"{up_w:.0%}"
        else:
            w_bar = "50%"
        trade_rows += (
            f"<tr><td class='mono'>{ts}</td>"
            f"<td class='mono'>{slug_short}</td>"
            f"<td><span class='badge {side_cls}'>{side}</span></td>"
            f"<td class='mono'>${t['price']:.4f}</td>"
            f"<td>{t['shares']:.1f}</td>"
            f"<td>${t['cost']:.2f}</td>"
            f"<td class='mono'>{btc_p}</td>"
            f"<td>{w_bar}</td></tr>"
        )

    # Market summary rows
    mkt_rows = ""
    for m in data["mkt_summary"][:20]:
        slug_short = m["market_slug"][-12:] if m["market_slug"] else ""
        # Find resolution info
        result = "—"
        pnl_str = "—"
        pnl_cls = ""
        for r in data["resolved"]:
            if r["slug"] == m["market_slug"]:
                result = r.get("result", "—")
                pnl = r.get("pnl", 0)
                pnl_str = f"${pnl:+,.2f}"
                pnl_cls = "green" if pnl >= 0 else "red"
                break
        mkt_rows += (
            f"<tr><td class='mono'>{slug_short}</td>"
            f"<td>{m['cnt']}</td>"
            f"<td>${m.get('up_spent', 0):.2f}</td>"
            f"<td>${m.get('down_spent', 0):.2f}</td>"
            f"<td>${m['spent']:.2f}</td>"
            f"<td>{result}</td>"
            f"<td class='{pnl_cls}'>{pnl_str}</td></tr>"
        )

    # Resolved rows
    resolved_rows = ""
    for r in data["resolved"]:
        slug_short = r["slug"][-12:] if r["slug"] else ""
        result = r.get("result", "—")
        pnl = r.get("pnl", 0)
        pnl_cls = "green" if pnl >= 0 else "red"
        badge = '<span class="badge badge-win">WIN</span>' if pnl > 0 else '<span class="badge badge-loss">LOSS</span>'
        title = r.get("title", slug_short)[:50]
        resolved_rows += (
            f"<tr><td>{title}</td>"
            f"<td>{badge} {result}</td>"
            f"<td class='{pnl_cls} mono'>${pnl:+,.2f}</td></tr>"
        )

    if not trade_rows:
        trade_rows = "<tr><td colspan='8' style='color:#8b949e;text-align:center'>等待交易…</td></tr>"
    if not mkt_rows:
        mkt_rows = "<tr><td colspan='7' style='color:#8b949e;text-align:center'>暂无市场</td></tr>"
    if not resolved_rows:
        resolved_rows = "<tr><td colspan='3' style='color:#8b949e;text-align:center'>暂无结算</td></tr>"

    # Use simple string replacement instead of .format() to avoid CSS brace conflicts
    html = HTML
    html = html.replace("{{balance}}", f"${data['balance']:,.2f}")
    html = html.replace("{{total_pnl}}", f"${data['total_pnl']:+,.2f}")
    html = html.replace("{{pnl_class}}", pnl_class)
    html = html.replace("{{total_trades}}", str(data["total_trades"]))
    html = html.replace("{{total_spent}}", f"${data['total_spent']:,.2f}")
    html = html.replace("{{btc_price}}", btc_str)
    html = html.replace("{{btc_change}}", btc_change)
    html = html.replace("{{trade_rows}}", trade_rows)
    html = html.replace("{{mkt_rows}}", mkt_rows)
    html = html.replace("{{resolved_rows}}", resolved_rows)
    html = html.replace("{{refresh_time}}", now.strftime("%H:%M:%S"))
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
            try:
                html = render()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode())
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(f"Error: {e}".encode())


def main():
    global DB_PATH, PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()
    DB_PATH = Path(args.db)
    PORT = args.port
    server = http.server.HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"🎯 Hermes BTC 5M Dashboard: http://0.0.0.0:{PORT}  DB={DB_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
