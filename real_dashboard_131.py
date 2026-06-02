#!/usr/bin/env python3
"""BTC Polymarket Predictor — Real Trading Dashboard.

Reads from real_trades table. Zero-dependency, built-in HTTP server.

Usage:
    python3 real_dashboard.py [--port PORT]
"""

import json, sqlite3, os, math, time, re
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
DB_PATH = os.path.join(DATA_DIR, 'btc_predictor.db')
PORT = int(os.environ.get('DASHBOARD_PORT', 8765))


def get_db():
    conn = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def safe_float(val, default=0.0):
    if val is None: return default
    try:
        f = float(val)
        return default if math.isnan(f) or math.isinf(f) else f
    except (ValueError, TypeError):
        return default


def parse_btc_window(market_slug: str):
    if not market_slug: return None, None, None, None
    m = re.search(r'(\d{9,11})$', market_slug)
    if not m: return None, None, None, None
    try:
        end_ts = int(m.group(1))
        start_ts = end_ts - 300
        start_dt = datetime.fromtimestamp(start_ts)
        end_dt = datetime.fromtimestamp(end_ts)
        return start_dt.strftime('%H:%M'), end_dt.strftime('%H:%M'), start_ts, end_ts
    except (ValueError, OSError):
        return None, None, None, None


# ============================================================================
# Data queries — adapted for real_trades
# ============================================================================

def get_recent_trades(limit=50):
    try:
        conn = get_db()
        rows = conn.execute(f"""
            SELECT id, timestamp, order_id, direction, market_slug,
                   market_question, price, size, side, edge, our_confidence,
                   status, filled_size, filled_price, pnl, cycle
            FROM real_trades
            ORDER BY id DESC LIMIT {limit}
        """).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_daily_stats():
    try:
        conn = get_db()
        rows = conn.execute("""
            SELECT
                DATE(timestamp) as day,
                COUNT(*) as trades,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl <= 0 AND status='resolved' THEN 1 ELSE 0 END) as losses,
                SUM(pnl) as total_pnl,
                SUM(size) as total_volume,
                AVG(our_confidence) as avg_confidence,
                AVG(edge) as avg_edge
            FROM real_trades
            WHERE status = 'resolved'
            GROUP BY DATE(timestamp)
            ORDER BY day DESC LIMIT 30
        """).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_hourly_stats():
    try:
        conn = get_db()
        rows = conn.execute("""
            SELECT
                strftime('%H', timestamp) as hour,
                COUNT(*) as trades,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl <= 0 AND status='resolved' THEN 1 ELSE 0 END) as losses,
                SUM(pnl) as total_pnl,
                AVG(our_confidence) as avg_confidence,
                AVG(edge) as avg_edge
            FROM real_trades
            WHERE status = 'resolved'
            GROUP BY strftime('%H', timestamp)
            ORDER BY hour
        """).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_summary():
    try:
        conn = get_db()
        row = conn.execute("""
            SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl <= 0 AND status='resolved' THEN 1 ELSE 0 END) as losses,
                SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) as open_positions,
                SUM(pnl) as total_pnl,
                SUM(size) as total_volume,
                AVG(CASE WHEN status='resolved' THEN
                    CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END
                END) as win_rate,
                AVG(edge) as avg_edge,
                AVG(our_confidence) as avg_confidence
            FROM real_trades
        """).fetchone()
        conn.close()
        return dict(row) if row else {}
    except Exception:
        return {}


# ============================================================================
# HTML Generation
# ============================================================================

CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
       background: #0d1117; color: #c9d1d9; padding: 20px; }
h1 { color: #3fb950; font-size: 1.5em; margin-bottom: 5px; }
.badge { display: inline-block; background: #3fb950; color: #000; font-size: 0.65em;
         padding: 2px 8px; border-radius: 10px; vertical-align: middle; margin-left: 8px; font-weight: bold; }
h2 { color: #8b949e; font-size: 1.1em; margin: 20px 0 10px 0; border-bottom: 1px solid #21262d; padding-bottom: 5px; }
.subtitle { color: #8b949e; font-size: 0.85em; margin-bottom: 20px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; margin-bottom: 20px; }
.card { background: #161b22; border: 1px solid #21262d; border-radius: 6px; padding: 12px 15px; }
.card .label { font-size: 0.75em; color: #8b949e; text-transform: uppercase; }
.card .value { font-size: 1.4em; font-weight: bold; margin-top: 3px; }
.card .sub { font-size: 0.75em; color: #8b949e; }
.positive { color: #3fb950; }
.negative { color: #f85149; }
.neutral { color: #d2991d; }
table { width: 100%; border-collapse: collapse; font-size: 0.82em; margin-bottom: 15px; }
th { text-align: left; padding: 6px 8px; background: #161b22; color: #8b949e; font-weight: 600;
     border-bottom: 1px solid #30363d; position: sticky; top: 0; }
td { padding: 5px 8px; border-bottom: 1px solid #21262d; }
tr:hover { background: #1c2129; }
.bar-container { background: #21262d; border-radius: 3px; height: 16px; overflow: hidden; }
.bar-fill { height: 100%; border-radius: 3px; transition: width 0.3s; }
.bar-win { background: #3fb950; }
.bar-loss { background: #f85149; }
.refresh { color: #8b949e; font-size: 0.75em; text-align: center; margin-top: 10px; }
.win { color: #3fb950; }
.loss { color: #f85149; }
.open { color: #d2991d; }
.window-tag { display: inline-block; background: #1a5c3a; color: #3fb950; padding: 1px 6px; border-radius: 3px; font-size: 0.85em; }
"""


def render():
    summary = get_summary()
    recent = get_recent_trades(50)
    daily = get_daily_stats()
    hourly = get_hourly_stats()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="5">
<title>BTC Polymarket — Live Trading</title>
<style>{CSS}</style>
</head>
<body>
<h1>🔮 BTC Polymarket Predictor <span class="badge">LIVE</span></h1>
<div class="subtitle">Real Trading Dashboard · Updated: {now} · Auto-refresh: 5s</div>

<div class="grid">
    <div class="card">
        <div class="label">Total P&L</div>
        <div class="value {classify_num(safe_float(summary.get('total_pnl', 0)))}">
            {fmt_pnl(safe_float(summary.get('total_pnl', 0)))}
        </div>
    </div>
    <div class="card">
        <div class="label">Total Trades</div>
        <div class="value">{summary.get('total_trades', 0)}</div>
        <div class="sub">Open: {summary.get('open_positions', 0)}</div>
    </div>
    <div class="card">
        <div class="label">Win Rate</div>
        <div class="value {classify_pct(safe_float(summary.get('win_rate', 0)))}">
            {safe_float(summary.get('win_rate', 0))*100:.1f}%
        </div>
        <div class="sub">W:{summary.get('wins', 0)} L:{summary.get('losses', 0)}</div>
    </div>
    <div class="card">
        <div class="label">Volume</div>
        <div class="value">${safe_float(summary.get('total_volume', 0)):,.0f}</div>
    </div>
    <div class="card">
        <div class="label">Avg Edge</div>
        <div class="value {classify_num(safe_float(summary.get('avg_edge', 0))*100)}">
            {safe_float(summary.get('avg_edge', 0))*100:.1f}%
        </div>
    </div>
    <div class="card">
        <div class="label">Avg Confidence</div>
        <div class="value">{safe_float(summary.get('avg_confidence', 0))*100:.1f}%</div>
    </div>
</div>

<h2>📋 Recent Trades</h2>
<table>
<thead>
<tr>
    <th>#</th><th>Time</th><th>BT窗口</th><th>Dir</th><th>Size</th>
    <th>Price</th><th>Edge</th><th>Conf</th><th>Cycle</th><th>P&L</th><th>Status</th>
</tr>
</thead>
<tbody>
{render_trade_rows(recent)}
</tbody>
</table>

<h2>📅 Daily Breakdown</h2>
<table>
<thead>
<tr><th>Date</th><th>Trades</th><th>Wins</th><th>Losses</th><th>Win Rate</th><th>P&L</th><th>Volume</th><th>Avg Edge</th></tr>
</thead>
<tbody>
{render_daily_rows(daily)}
</tbody>
</table>

<h2>🕐 Hourly Breakdown</h2>
<table>
<thead>
<tr><th>Hour</th><th>Trades</th><th>Wins</th><th>Losses</th><th>Win Rate</th><th>P&L</th><th>Avg Edge</th><th>Win % Bar</th></tr>
</thead>
<tbody>
{render_hourly_rows(hourly)}
</tbody>
</table>

<div class="refresh">Auto-refreshing every 5 seconds · Last: {now}</div>
</body>
</html>"""
    return html


def classify_num(val):
    return 'positive' if val > 0 else 'negative' if val < 0 else ''


def classify_pct(val):
    return 'positive' if val > 0.5 else 'negative' if val < 0.5 else 'neutral'


def fmt_pnl(val):
    return f"+${val:,.2f}" if val >= 0 else f"-${abs(val):,.2f}"


def fmt_time(ts):
    try:
        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
        return dt.astimezone().strftime('%m-%d %H:%M')
    except Exception:
        return ts[:16] if ts else ''


def fmt_btc_window(market_slug):
    start_str, end_str, _, _ = parse_btc_window(market_slug)
    if start_str and end_str:
        return f'<span class="window-tag">{start_str}-{end_str}</span>'
    return '—'


def render_trade_rows(trades):
    rows = []
    for t in trades:
        status = t.get('status', 'open')
        pnl = safe_float(t.get('pnl', 0))
        status_class = 'win' if pnl > 0 else 'loss' if status == 'resolved' else 'open'
        window_html = fmt_btc_window(t.get('market_slug', ''))
        rows.append(f"""<tr>
            <td>{t.get('id', '')}</td>
            <td>{fmt_time(t.get('timestamp', ''))}</td>
            <td>{window_html}</td>
            <td>{t.get('direction', '')}</td>
            <td>${safe_float(t.get('size', 0)):,.2f}</td>
            <td>${safe_float(t.get('price', 0)):.3f}</td>
            <td class="{classify_num(safe_float(t.get('edge', 0))*100)}">{safe_float(t.get('edge', 0))*100:.1f}%</td>
            <td>{safe_float(t.get('our_confidence', 0))*100:.1f}%</td>
            <td>{t.get('cycle', '')}</td>
            <td class="{classify_num(pnl)}">{fmt_pnl(pnl)}</td>
            <td class="{status_class}">{status.upper()}</td>
        </tr>""")
    return '\n'.join(rows) if rows else '<tr><td colspan="11">No trades yet</td></tr>'


def render_daily_rows(daily):
    rows = []
    for d in daily:
        trades = d.get('trades', 0) or 0
        wins = d.get('wins', 0) or 0
        losses = d.get('losses', 0) or 0
        wr = wins / max(trades, 1) * 100
        pnl = safe_float(d.get('total_pnl', 0))
        rows.append(f"""<tr>
            <td>{d.get('day', '')}</td>
            <td>{trades}</td><td>{wins}</td><td>{losses}</td>
            <td class="{classify_pct(wr/100)}">{wr:.1f}%</td>
            <td class="{classify_num(pnl)}">{fmt_pnl(pnl)}</td>
            <td>${safe_float(d.get('total_volume', 0)):,.0f}</td>
            <td>{safe_float(d.get('avg_edge', 0))*100:.1f}%</td>
        </tr>""")
    return '\n'.join(rows) if rows else '<tr><td colspan="8">No data yet</td></tr>'


def render_hourly_rows(hourly):
    if not hourly:
        return '<tr><td colspan="8">No data yet</td></tr>'
    max_trades = max(h.get('trades', 0) or 1 for h in hourly)
    rows = []
    for h in hourly:
        hour = h.get('hour', '??')
        trades = h.get('trades', 0) or 0
        wins = h.get('wins', 0) or 0
        losses = h.get('losses', 0) or 0
        wr = wins / max(trades, 1) * 100
        pnl = safe_float(h.get('total_pnl', 0))
        bar_pct = trades / max(max_trades, 1) * 100
        rows.append(f"""<tr>
            <td>{hour}:00</td>
            <td>{trades}</td><td>{wins}</td><td>{losses}</td>
            <td class="{classify_pct(wr/100)}">{wr:.1f}%</td>
            <td class="{classify_num(pnl)}">{fmt_pnl(pnl)}</td>
            <td>{safe_float(h.get('avg_edge', 0))*100:.1f}%</td>
            <td><div class="bar-container"><div class="bar-fill bar-{'win' if wr >= 50 else 'loss'}" style="width:{max(bar_pct, 5)}%"></div></div></td>
        </tr>""")
    return '\n'.join(rows)


# ============================================================================
# HTTP Server
# ============================================================================

class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ('/', '/index.html'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(render().encode('utf-8'))
        elif self.path == '/api/summary':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(get_summary(), default=str).encode())
        elif self.path == '/api/trades':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(get_recent_trades(100), default=str).encode())
        elif self.path == '/api/daily':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(get_daily_stats(), default=str).encode())
        elif self.path == '/api/hourly':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(get_hourly_stats(), default=str).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Real Trading Dashboard')
    parser.add_argument('--port', type=int, default=PORT)
    args = parser.parse_args()

    server = HTTPServer(('0.0.0.0', args.port), DashboardHandler)
    print(f"\n🚀 LIVE Trading Dashboard at http://0.0.0.0:{args.port}")
    print(f"   Press Ctrl+C to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Dashboard stopped.")


if __name__ == '__main__':
    main()
