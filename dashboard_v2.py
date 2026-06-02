"""Web Dashboard v2 - WebSocket Real-time BTC Predictor Dashboard"""

import json
import time
import logging
import math
import sqlite3
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

from config import DB_PATH

logger = logging.getLogger(__name__)
app = FastAPI(title="BTC Realtime Predictor", version="2.0.0")


def sanitize(obj):
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize(v) for v in obj]
    elif isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    return obj


def get_db():
    return sqlite3.connect(DB_PATH)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return _html()


@app.get("/api/status")
async def api_status():
    conn = get_db()
    # Latest prediction
    row = conn.execute("""
        SELECT timestamp, mid_price, spread, ob_imbalance, cvd, buy_sell_ratio,
               funding_rate, mark_price, direction, confidence, score, cycle
        FROM realtime_predictions ORDER BY id DESC LIMIT 1
    """).fetchone()
    
    # Stats
    stats = conn.execute("""
        SELECT COUNT(*), AVG(confidence), AVG(mid_price),
               SUM(CASE WHEN direction=1 THEN 1 ELSE 0 END),
               SUM(CASE WHEN direction=-1 THEN 1 ELSE 0 END),
               AVG(ob_imbalance), AVG(cvd)
        FROM realtime_predictions
    """).fetchone()
    conn.close()
    
    result = {
        "total_predictions": stats[0] if stats else 0,
        "avg_confidence": stats[1] if stats else 0,
        "avg_price": stats[2] if stats else 0,
        "up_count": stats[3] if stats else 0,
        "dn_count": stats[4] if stats else 0,
        "avg_imbalance": stats[5] if stats else 0,
        "avg_cvd": stats[6] if stats else 0,
    }
    
    if row:
        result["latest"] = {
            "timestamp": row[0],
            "price": row[1],
            "spread": row[2],
            "ob_imbalance": row[3],
            "cvd": row[4],
            "buy_sell_ratio": row[5],
            "funding_rate": row[6],
            "mark_price": row[7],
            "direction": row[8],
            "confidence": row[9],
            "score": row[10],
            "cycle": row[11],
        }
    
    return JSONResponse(content=sanitize(result))


@app.get("/api/predictions")
async def api_predictions(limit: int = 20):
    conn = get_db()
    rows = conn.execute("""
        SELECT timestamp, mid_price, direction, confidence, score, cycle
        FROM realtime_predictions ORDER BY id DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    
    predictions = []
    for row in rows:
        predictions.append({
            "timestamp": row[0],
            "price": row[1],
            "direction": "UP" if row[2] == 1 else "DN",
            "confidence": row[3],
            "score": row[4],
            "cycle": row[5],
        })
    
    return JSONResponse(content=sanitize(predictions))


@app.get("/api/signals")
async def api_signals(limit: int = 10):
    conn = get_db()
    rows = conn.execute("""
        SELECT timestamp, signals, direction, confidence, cycle
        FROM realtime_predictions ORDER BY id DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    
    signals = []
    for row in rows:
        sig_list = json.loads(row[1]) if row[1] else []
        signals.append({
            "timestamp": row[0],
            "signals": sig_list,
            "direction": "UP" if row[2] == 1 else "DN",
            "confidence": row[3],
            "cycle": row[4],
        })
    
    return JSONResponse(content=sanitize(signals))


def _html():
    return '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BTC 5-Min Realtime Predictor</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0a0e17; color: #e1e8f0; }
.container { max-width: 1400px; margin: 0 auto; padding: 20px; }
.header { text-align: center; padding: 20px 0; border-bottom: 1px solid #1e2a3a; margin-bottom: 20px; }
.header h1 { font-size: 24px; color: #00d4ff; }
.header .subtitle { color: #6b7a8d; font-size: 14px; margin-top: 5px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-bottom: 20px; }
.card { background: #111827; border: 1px solid #1e2a3a; border-radius: 12px; padding: 20px; }
.card h3 { color: #6b7a8d; font-size: 13px; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 10px; }
.card .value { font-size: 28px; font-weight: 700; }
.card .sub { color: #6b7a8d; font-size: 13px; margin-top: 5px; }
.up { color: #00e676; }
.dn { color: #ff5252; }
.neutral { color: #ffd740; }
.section { background: #111827; border: 1px solid #1e2a3a; border-radius: 12px; padding: 20px; margin-bottom: 20px; }
.section h2 { color: #00d4ff; font-size: 18px; margin-bottom: 15px; }
table { width: 100%; border-collapse: collapse; }
th { text-align: left; color: #6b7a8d; font-size: 12px; padding: 8px 12px; border-bottom: 1px solid #1e2a3a; }
td { padding: 10px 12px; border-bottom: 1px solid #1a2332; font-size: 14px; }
.signal-tag { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; margin: 2px; }
.signal-buy { background: rgba(0,230,118,0.15); color: #00e676; }
.signal-sell { background: rgba(255,82,82,0.15); color: #ff5252; }
.signal-info { background: rgba(0,212,255,0.15); color: #00d4ff; }
.signal-warn { background: rgba(255,215,64,0.15); color: #ffd740; }
.progress-bar { height: 8px; background: #1a2332; border-radius: 4px; overflow: hidden; margin-top: 8px; }
.progress-fill { height: 100%; border-radius: 4px; transition: width 0.3s; }
.status-dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 8px; }
.status-online { background: #00e676; box-shadow: 0 0 8px #00e676; }
.status-offline { background: #ff5252; }
.meta { color: #6b7a8d; font-size: 12px; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.5} }
.pulse { animation: pulse 2s infinite; }
</style>
</head>
<body>
<div class="container">
<div class="header">
<h1>🔮 BTC 5-Minute Realtime Predictor</h1>
<div class="subtitle">WebSocket OKX | Order Flow + Volatility + Mean Reversion + Event Driven</div>
</div>

<div class="grid" id="stats-grid">
<div class="card">
<h3>💰 BTC Price</h3>
<div class="value" id="price">--</div>
<div class="sub">Spread: <span id="spread">--</span></div>
</div>
<div class="card">
<h3>🎯 Prediction</h3>
<div class="value" id="direction">--</div>
<div class="sub">Confidence: <span id="confidence">--</span></div>
<div class="progress-bar"><div class="progress-fill" id="conf-bar"></div></div>
</div>
<div class="card">
<h3>📈 Order Book</h3>
<div class="value" id="imbalance">--</div>
<div class="sub">Imbalance</div>
</div>
<div class="card">
<h3>💹 CVD</h3>
<div class="value" id="cvd">--</div>
<div class="sub">Buy/Sell: <span id="bs-ratio">--</span></div>
</div>
<div class="card">
<h3>💰 Funding Rate</h3>
<div class="value" id="funding">--</div>
<div class="sub">Mark: <span id="mark">--</span></div>
</div>
<div class="card">
<h3>📊 Stats</h3>
<div class="value" id="total-preds">0</div>
<div class="sub">UP: <span id="up-count">0</span> | DN: <span id="dn-count">0</span></div>
</div>
</div>

<div class="section">
<h2>📡 Live Signals</h2>
<div id="signals-container"><p class="meta">Loading...</p></div>
</div>

<div class="section">
<h2>📋 Recent Predictions</h2>
<table>
<thead><tr><th>Cycle</th><th>Time</th><th>Price</th><th>Direction</th><th>Confidence</th><th>Score</th></tr></thead>
<tbody id="pred-table"><tr><td colspan="6" class="meta">Loading...</td></tr></tbody>
</table>
</div>

<div class="section">
<h2>🔧 System Status</h2>
<div id="system-status"><p class="meta">Loading...</p></div>
</div>
</div>

<script>
const API = '';
let lastCycle = 0;

function formatTime(iso) {
    if (!iso) return '--';
    const d = new Date(iso);
    return d.toLocaleTimeString('en-US', {hour12: false}) + ' UTC';
}

function update() {
    fetch(API + '/api/status')
        .then(r => r.json())
        .then(data => {
            // Stats
            document.getElementById('total-preds').textContent = data.total_predictions || 0;
            document.getElementById('up-count').textContent = data.up_count || 0;
            document.getElementById('dn-count').textContent = data.dn_count || 0;
            
            if (data.latest) {
                const l = data.latest;
                const dirEl = document.getElementById('direction');
                dirEl.textContent = l.direction === 1 ? '⬆ UP' : '⬇ DN';
                dirEl.className = 'value ' + (l.direction === 1 ? 'up' : 'dn');
                
                document.getElementById('price').textContent = '$' + (l.price || 0).toLocaleString(undefined, {maximumFractionDigits:2});
                document.getElementById('spread').textContent = '$' + (l.spread || 0).toFixed(2);
                document.getElementById('confidence').textContent = ((l.confidence || 0.5) * 100).toFixed(1) + '%';
                
                const bar = document.getElementById('conf-bar');
                bar.style.width = ((l.confidence || 0.5) * 100) + '%';
                bar.style.background = l.direction === 1 ? '#00e676' : '#ff5252';
                
                const imb = l.ob_imbalance || 0;
                const imbEl = document.getElementById('imbalance');
                imbEl.textContent = (imb > 0 ? '+' : '') + (imb * 100).toFixed(2) + '%';
                imbEl.className = 'value ' + (imb > 0.1 ? 'up' : imb < -0.1 ? 'dn' : 'neutral');
                
                document.getElementById('cvd').textContent = (l.cvd || 0).toFixed(4);
                document.getElementById('cvd').className = 'value ' + ((l.cvd || 0) > 0 ? 'up' : 'dn');
                document.getElementById('bs-ratio').textContent = (l.buy_sell_ratio || 0).toFixed(4);
                
                const fr = l.funding_rate || 0;
                document.getElementById('funding').textContent = (fr * 100).toFixed(6) + '%';
                document.getElementById('funding').className = 'value ' + (fr > 0 ? 'dn' : fr < 0 ? 'up' : 'neutral');
                document.getElementById('mark').textContent = '$' + (l.mark_price || 0).toLocaleString(undefined, {maximumFractionDigits:0});
                
                if (l.cycle !== lastCycle) {
                    lastCycle = l.cycle;
                    updateSignals();
                    updateTable();
                }
            }
            
            // System status
            const statusEl = document.getElementById('system-status');
            statusEl.innerHTML = '<p><span class="status-dot status-online"></span> WebSocket OKX Connected</p>' +
                '<p class="meta">Avg Confidence: ' + ((data.avg_confidence || 0) * 100).toFixed(1) + '% | ' +
                'Avg Imbalance: ' + ((data.avg_imbalance || 0) * 100).toFixed(2) + '% | ' +
                'Avg CVD: ' + (data.avg_cvd || 0).toFixed(4) + '</p>';
        })
        .catch(e => console.error('Status error:', e));
}

function updateSignals() {
    fetch(API + '/api/signals?limit=5')
        .then(r => r.json())
        .then(data => {
            const container = document.getElementById('signals-container');
            if (!data.length) { container.innerHTML = '<p class="meta">No signals yet</p>'; return; }
            
            let html = '';
            data.forEach(item => {
                html += '<div style="margin-bottom:12px;padding:10px;background:#0d1321;border-radius:8px;">';
                html += '<div class="meta">' + formatTime(item.timestamp) + ' | Cycle #' + item.cycle + ' | ';
                html += '<span style="color:' + (item.direction === 'UP' ? '#00e676' : '#ff5252') + '">' + item.direction + '</span>';
                html += ' (' + ((item.confidence || 0.5) * 100).toFixed(1) + '%)</div>';
                html += '<div style="margin-top:6px;">';
                (item.signals || []).forEach(s => {
                    let cls = 'signal-info';
                    if (s.includes('buy') || s.includes('bid') || s.includes('UP') || s.includes('reversion up')) cls = 'signal-buy';
                    else if (s.includes('sell') || s.includes('ask') || s.includes('DN') || s.includes('reversion down')) cls = 'signal-sell';
                    else if (s.includes('⚠') || s.includes('squeeze') || s.includes('disagree')) cls = 'signal-warn';
                    html += '<span class="signal-tag ' + cls + '">' + s + '</span>';
                });
                html += '</div></div>';
            });
            container.innerHTML = html;
        });
}

function updateTable() {
    fetch(API + '/api/predictions?limit=15')
        .then(r => r.json())
        .then(data => {
            const tbody = document.getElementById('pred-table');
            if (!data.length) { tbody.innerHTML = '<tr><td colspan="6" class="meta">No predictions yet</td></tr>'; return; }
            
            let html = '';
            data.forEach(p => {
                const dirClass = p.direction === 'UP' ? 'up' : 'dn';
                html += '<tr>';
                html += '<td>#' + p.cycle + '</td>';
                html += '<td class="meta">' + formatTime(p.timestamp) + '</td>';
                html += '<td>$' + (p.price || 0).toLocaleString(undefined, {maximumFractionDigits:0}) + '</td>';
                html += '<td class="' + dirClass + '"><strong>' + p.direction + '</strong></td>';
                html += '<td>' + ((p.confidence || 0.5) * 100).toFixed(1) + '%</td>';
                html += '<td>' + (p.score || 0).toFixed(4) + '</td>';
                html += '</tr>';
            });
            tbody.innerHTML = html;
        });
}

// Initial load
update();
setInterval(update, 5000);
</script>
</body>
</html>'''


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logger.info("Starting dashboard on port 8765...")
    uvicorn.run(app, host="0.0.0.0", port=8765, log_level="warning")
