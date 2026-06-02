#!/usr/bin/env python3
"""
📊 Real Trading Dashboard — 164 实盘看板

Shows live copy-trading status for WeatherHK + Annica + Claude Signal.
Reads from copybot logs + ClobClient balance.

Usage: python3 real_dashboard.py [--port 8766]
"""

import json, os, re, sqlite3, time
from pathlib import Path
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
PORT = 8766
REFRESH_SEC = 15

PAGE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>📊 Real Trading Dashboard</title>
<style>
  :root {
    --bg: #0d1117; --card: #161b22; --border: #30363d;
    --text: #c9d1d9; --muted: #8b949e; --green: #3fb950;
    --red: #f85149; --blue: #58a6ff; --orange: #d2991d; --purple: #a371f7;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; padding: 20px; max-width: 1200px; margin: 0 auto; }
  h2 { font-size: 18px; margin: 20px 0 10px; display: flex; align-items: center; gap: 8px; }
  h2 .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
  .dot-green { background: var(--green); animation: pulse 2s infinite; }
  .dot-red { background: var(--red); }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; margin-bottom: 16px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; }
  .card .label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }
  .card .value { font-size: 22px; font-weight: 700; margin: 4px 0; }
  .card .sub { font-size: 11px; color: var(--muted); }
  .val-good { color: var(--green); } .val-bad { color: var(--red); }
  .val-neutral { color: var(--blue); } .val-warn { color: var(--orange); }
  table { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 10px; }
  th { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); color: var(--muted); font-size: 10px; text-transform: uppercase; }
  td { padding: 6px 10px; border-bottom: 1px solid var(--border); }
  .refresh { font-size: 11px; color: var(--muted); text-align: right; margin-top: 12px; }
  .section { border-left: 3px solid var(--blue); padding-left: 12px; margin-bottom: 20px; }
  .section.weather { border-left-color: var(--green); }
  .section.annica { border-left-color: var(--purple); }
  .section.claude { border-left-color: var(--orange); }
</style>
</head>
<body>
<h1>📊 Real Trading Dashboard <span style="font-size:13px;color:var(--muted);font-weight:400">— 164 Server</span></h1>

<div class="cards" id="cards"></div>

<div class="section weather">
  <h2><span class="dot dot-green"></span>🌤 WeatherHK 实盘</h2>
  <div class="cards" id="whk-cards"></div>
</div>

<div class="section annica">
  <h2><span class="dot dot-green"></span>🐦 Annica 实盘</h2>
  <div class="cards" id="ann-cards"></div>
</div>

<div class="section claude">
  <h2><span class="dot dot-green"></span>🤖 Claude Bot 信号</h2>
  <div class="cards" id="claude-cards"></div>
  <div id="claude-acc"></div>
</div>

<div class="refresh" id="refresh"></div>

<script>
const R = __REFRESH__;

function load() {
  fetch('/api/status').then(r => r.json()).then(d => {
    // Overall cards
    document.getElementById('cards').innerHTML = `
      <div class="card"><div class="label">💰 余额</div><div class="value val-neutral">$${d.balance_usdc||'—'}</div></div>
      <div class="card"><div class="label">📦 持仓</div><div class="value val-neutral">${d.holds||0}</div></div>
      <div class="card"><div class="label">⏳ 待成交</div><div class="value val-warn">${d.pending||0}</div></div>
      <div class="card"><div class="label">🔄 最后更新</div><div class="value" style="font-size:14px;">${d.updated||'—'}</div></div>
    `;

    // WeatherHK
    const w = d.weatherhk || {};
    document.getElementById('whk-cards').innerHTML = `
      <div class="card"><div class="label">余额</div><div class="value val-neutral">$${w.bal||'—'}</div></div>
      <div class="card"><div class="label">持仓</div><div class="value val-neutral">${w.holds||0}</div></div>
      <div class="card"><div class="label">追踪事件</div><div class="value val-neutral">${w.tracked||0}</div></div>
      <div class="card"><div class="label">已执行</div><div class="value val-good">${w.executed||0}</div></div>
    `;

    // Annica
    const a = d.annica || {};
    document.getElementById('ann-cards').innerHTML = `
      <div class="card"><div class="label">余额</div><div class="value val-neutral">$${a.bal||'—'}</div></div>
      <div class="card"><div class="label">持仓</div><div class="value val-neutral">${a.holds||0}</div></div>
      <div class="card"><div class="label">追踪事件</div><div class="value val-neutral">${a.tracked||0}</div></div>
      <div class="card"><div class="label">已执行</div><div class="value val-good">${a.executed||0}</div></div>
    `;

    // Claude
    const c = d.claude || {};
    const sigColor = {'STRONG_DOWN':'val-bad','DOWN_BIAS':'val-bad','NEUTRAL':'val-neutral','UP_BIAS':'val-good','STRONG_UP':'val-good'};
    const sigEmoji = {'STRONG_DOWN':'🔴','DOWN_BIAS':'🟠','NEUTRAL':'⚪','UP_BIAS':'🟢','STRONG_UP':'🟢'};
    document.getElementById('claude-cards').innerHTML = `
      <div class="card"><div class="label">信号</div><div class="value ${sigColor[c.signal]||'val-neutral'}">${sigEmoji[c.signal]||''} ${c.signal||'—'}</div></div>
      <div class="card"><div class="label">Up</div><div class="value val-good">${c.up_pct||0}%</div></div>
      <div class="card"><div class="label">Down</div><div class="value val-bad">${c.down_pct||0}%</div></div>
      <div class="card"><div class="label">交易数</div><div class="value val-neutral">${c.trades||0}</div></div>
    `;

    document.getElementById('refresh').textContent = 'Last refresh: ' + new Date().toLocaleTimeString();
  });
}
load();
setInterval(load, R * 1000);
</script>
</body>
</html>""".replace("__REFRESH__", str(REFRESH_SEC))


def parse_copybot_log(bot_name):
    """Extract latest status from copybot log."""
    log_path = LOG_DIR / f"{bot_name}_copy.log"
    if not log_path.exists():
        return {"bal": "—", "holds": 0, "tracked": 0, "executed": 0, "running": False}

    try:
        with open(log_path) as f:
            lines = f.readlines()
        
        # Parse last status line: "C#NNN | holds=X pending=Y cache=Z mkts | bal=$X.XX | tracked=N events"
        result = {"bal": "—", "holds": 0, "pending": 0, "tracked": 0, "executed": 0, "running": True}
        
        for line in reversed(lines):
            m = re.search(r'holds=(\d+)\s+pending=(\d+).*?\bbal=\$([\d.]+).*?tracked=(\d+)', line)
            if m:
                result["holds"] = int(m.group(1))
                result["pending"] = int(m.group(2))
                result["bal"] = m.group(3)
                result["tracked"] = int(m.group(4))
                
                # Count executed trades
                exec_count = sum(1 for l in lines if '✅ EXECUTED' in l or '🎫 COPYING' in l or '🔻 CLOSING' in l or 'BUY placed' in l or 'SELL placed' in l)
                result["executed"] = exec_count
                break
        
        return result
    except Exception:
        return {"bal": "—", "holds": 0, "tracked": 0, "executed": 0, "running": False}


def get_claude_signal():
    """Read Claude signal JSON."""
    signal_path = BASE_DIR / "data" / "claude_signal.json"
    if not signal_path.exists():
        return {"signal": "NO_DATA", "up_pct": 0, "down_pct": 0, "trades": 0}
    try:
        with open(signal_path) as f:
            d = json.load(f)
        return {
            "signal": d.get("signal", "?"),
            "up_pct": d.get("bias", {}).get("up_pct", 0),
            "down_pct": d.get("bias", {}).get("down_pct", 0),
            "trades": d.get("bias", {}).get("total_trades", 0),
        }
    except Exception:
        return {"signal": "ERROR", "up_pct": 0, "down_pct": 0, "trades": 0}


def get_status():
    """Build full status payload."""
    whk = parse_copybot_log("weatherhk")
    ann = parse_copybot_log("annica")
    claude = get_claude_signal()

    # Aggregate
    total_holds = whk.get("holds", 0) + ann.get("holds", 0)
    total_pending = whk.get("pending", 0) + ann.get("pending", 0)
    
    # Balance: same wallet for both
    balance = whk.get("bal", "—")

    return {
        "updated": datetime.now().strftime("%H:%M:%S"),
        "balance_usdc": balance,
        "holds": total_holds,
        "pending": total_pending,
        "weatherhk": whk,
        "annica": ann,
        "claude": claude,
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(get_status()).encode())
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(PAGE.encode())
    
    def log_message(self, *args):
        pass


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=PORT)
    args = p.parse_args()

    print(f"📊 Real Trading Dashboard")
    print(f"   http://localhost:{args.port}")
    print(f"   http://8.210.151.164:{args.port}")
    print(f"   Refresh: {REFRESH_SEC}s")
    server = HTTPServer(("0.0.0.0", args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Done")
