"""
📊 Dashboard Server — live trading dashboard for Market Maker Bot.
Serves at http://localhost:8766
"""

import json, sqlite3, http.server
from pathlib import Path
from datetime import datetime, timezone
from trade_journal import init_db, get_summary, get_recent_trades, get_balance_history

DB_PATH = Path(__file__).parent / "data" / "trade_journal.db"


def build_html(db: sqlite3.Connection) -> str:
    summary = get_summary(db)
    trades = get_recent_trades(db, 30)
    balances = get_balance_history(db, 60)

    trade_rows = ""
    for t in trades:
        emoji = {"pair": "🎉", "single_fill": "📥", "exit": "🚪",
                 "order_placed": "📝", "balance": "💰"}.get(t.get("type",""), "")
        profit_str = f'<span class="profit">${t["profit"]:+.2f}</span>' if t.get("profit") else ""
        market_str = (t.get("market") or "")[:35]
        purpose_str = (t.get("purpose") or "")[:45]
        trade_rows += f"""
        <tr>
            <td class="ts">{(t.get('ts') or '')[:19]}</td>
            <td>{emoji} {t.get('type','')}</td>
            <td class="market">{market_str}</td>
            <td>{t.get('side','') or ''}</td>
            <td class="num">{'${:.2f}'.format(t['price']) if t.get('price') else ''}</td>
            <td class="num">{'{:.0f}'.format(t['size']) if t.get('size') else ''}</td>
            <td class="num">{'${:.2f}'.format(t['cost']) if t.get('cost') else ''}</td>
            <td class="purpose">{purpose_str}</td>
            <td class="num">{'${:.2f}'.format(t['balance_after']) if t.get('balance_after') else ''}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🎯 Polymarket Market Maker</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', monospace; background: #0a0a0f; color: #e0e0e0; padding: 16px; }}
.header {{ display: flex; gap: 16px; margin-bottom: 16px; flex-wrap: wrap; }}
.card {{ background: #14141f; border: 1px solid #2a2a3a; border-radius: 8px; padding: 12px 16px; min-width: 140px; }}
.card .label {{ font-size: 11px; color: #888; text-transform: uppercase; margin-bottom: 4px; }}
.card .value {{ font-size: 22px; font-weight: bold; }}
.card .sub {{ font-size: 13px; color: #aaa; }}
.profit-green {{ color: #4ade80; }}
.loss-red {{ color: #f87171; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 13px; }}
th {{ background: #1a1a2a; padding: 8px 6px; text-align: left; border-bottom: 2px solid #333; color: #aaa; font-size: 11px; }}
td {{ padding: 6px; border-bottom: 1px solid #1a1a2a; }}
td.ts {{ color: #666; font-size: 11px; white-space: nowrap; }}
td.market {{ max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
td.purpose {{ color: #888; font-size: 12px; }}
.section-title {{ font-size: 14px; font-weight: bold; color: #aaa; margin: 16px 0 8px; border-bottom: 1px solid #2a2a3a; padding-bottom: 4px; }}
.auto-refresh {{ font-size: 11px; color: #555; margin-top: 8px; text-align: center; }}
@media (max-width: 600px) {{ .card {{ min-width: 100px; }} }}
</style>
<script>setTimeout(function(){{ location.reload(); }}, 15000);</script>
</head>
<body>
<h2 style="margin-bottom:12px">🎯 Polymarket Market Maker</h2>

<div class="header">
    <div class="card">
        <div class="label">💰 余额</div>
        <div class="value">${summary['balance']:.2f}</div>
    </div>
    <div class="card">
        <div class="label">🎉 已完成对</div>
        <div class="value profit-green">{summary['pair_count']}</div>
        <div class="sub">${summary['pair_pnl']:+.2f}</div>
    </div>
    <div class="card">
        <div class="label">📥 单边持仓</div>
        <div class="value">{summary['open_singles']}</div>
        <div class="sub">待到期结算</div>
    </div>
    <div class="card">
        <div class="label">📊 净盈亏</div>
        <div class="value {'profit-green' if summary['net_pnl'] >= 0 else 'loss-red'}">${summary['net_pnl']:+.2f}</div>
    </div>
</div>

<div class="section-title">📋 交易记录 (最近30笔)</div>
<table>
<tr><th>时间</th><th>类型</th><th>市场</th><th>方向</th><th>价格</th><th>数量</th><th>成本</th><th>目的</th><th>余额</th></tr>
{trade_rows}
</table>

<div class="auto-refresh">⏱ 每 15 秒自动刷新 | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}</div>
</body>
</html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
            db.execute("PRAGMA journal_mode=WAL")
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f"DB error: {e}".encode())
            return

        if self.path == "/":
            try:
                html = build_html(db)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode())
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(f"Render error: {e}".encode())
        elif self.path == "/api/summary":
            data = get_summary(db)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())
        else:
            self.send_response(404)
            self.end_headers()
        db.close()


if __name__ == "__main__":
    init_db()
    server = http.server.HTTPServer(("0.0.0.0", 8766), Handler)
    print("🌐 Dashboard: http://localhost:8766")
    server.serve_forever()
