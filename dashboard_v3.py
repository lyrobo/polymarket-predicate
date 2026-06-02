"""Web Dashboard v3 - WebSocket Real-time BTC Predictor + Polymarket Edge"""

import json
import time
import logging
import math
import sqlite3
import os
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
import uvicorn

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
DB_PATH = f'{DATA_DIR}/btc_predictor.db'
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

logger = logging.getLogger(__name__)
app = FastAPI(title="BTC Realtime Predictor + Edge", version="3.0.0")


def sanitize(obj):
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize(v) for v in obj]
    elif isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    return obj


def get_db():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return _html()


@app.get("/api/status")
async def api_status():
    conn = get_db()

    # Latest prediction
    row = conn.execute("""
        SELECT timestamp, btc_price, up_price, down_price, edge,
               direction, our_confidence, score, action, market_slug
        FROM realtime_predictions ORDER BY id DESC LIMIT 1
    """).fetchone()

    # Stats - use our_confidence instead of confidence
    stats = conn.execute("""
        SELECT COUNT(*), AVG(our_confidence), AVG(btc_price),
               SUM(CASE WHEN direction=1 THEN 1 ELSE 0 END),
               SUM(CASE WHEN direction=-1 THEN 1 ELSE 0 END),
               AVG(edge), AVG(our_confidence)
        FROM realtime_predictions
    """).fetchone()

    # Latest edge
    edge_row = conn.execute("""
        SELECT timestamp, market_slug, market_question, market_up_price, market_down_price,
               our_direction, our_confidence, our_prob, market_price, edge, edge_pct,
               ev_per_share, bet_on, action, market_active, market_closed,
               market_volume, market_liquidity, recommendation, cycle
        FROM polymarket_edges ORDER BY id DESC LIMIT 1
    """).fetchone()

    # Edge stats
    edge_stats = conn.execute("""
        SELECT COUNT(*), AVG(edge),
               SUM(CASE WHEN ABS(edge) >= 0.03 THEN 1 ELSE 0 END),
               SUM(CASE WHEN action LIKE 'BUY_%' THEN 1 ELSE 0 END),
               SUM(CASE WHEN action = 'HOLD' THEN 1 ELSE 0 END)
        FROM polymarket_edges
    """).fetchone()


    result = {
        "total_predictions": stats[0] if stats else 0,
        "avg_confidence": stats[1] if stats else 0,
        "avg_price": stats[2] if stats else 0,
        "up_count": stats[3] if stats else 0,
        "dn_count": stats[4] if stats else 0,
        "avg_edge": stats[5] if stats else 0,
        "avg_our_confidence": stats[6] if stats else 0,
    }

    if row:
        result["latest"] = {
            "timestamp": row[0], "btc_price": row[1], "up_price": row[2],
            "down_price": row[3], "edge": row[4],
            "direction": row[5], "confidence": row[6], "score": row[7],
            "action": row[8], "market_slug": row[9],
        }

    if edge_row:
        result["latest_edge"] = {
            "timestamp": edge_row[0], "market_slug": edge_row[1],
            "market_question": edge_row[2],
            "market_up_price": edge_row[3], "market_down_price": edge_row[4],
            "our_direction": edge_row[5], "our_confidence": edge_row[6],
            "our_prob": edge_row[7], "market_price": edge_row[8],
            "edge": edge_row[9], "edge_pct": edge_row[10],
            "ev_per_share": edge_row[11], "bet_on": edge_row[12],
            "action": edge_row[13], "market_active": bool(edge_row[14]),
            "market_closed": bool(edge_row[15]),
            "market_volume": edge_row[16], "market_liquidity": edge_row[17],
            "recommendation": edge_row[18], "cycle": edge_row[19],
        }

    if edge_stats and edge_stats[0] > 0:
        result["edge_stats"] = {
            "total_edges": edge_stats[0],
            "avg_edge": edge_stats[1],
            "edge_3pct_count": edge_stats[2],
            "buy_signals": edge_stats[3],
            "hold_signals": edge_stats[4],
        }

    # UP strategy stats (UP-ONLY strategy — no DOWN betting)
    up_stats = conn.execute("""
        SELECT COUNT(*), SUM(CASE WHEN win=1 THEN 1 ELSE 0 END)
        FROM sim_trades WHERE direction='Up' AND status='resolved'
    """).fetchone()
    if up_stats and up_stats[0] > 0:
        result["up_stats"] = {
            "total_trades": up_stats[0],
            "wins": up_stats[1],
            "win_rate": round(up_stats[1] / up_stats[0] * 100, 2),
        }

    return JSONResponse(content=sanitize(result))


@app.get("/api/predictions")
async def api_predictions(limit: int = 20):
    conn = get_db()
    rows = conn.execute("""
        SELECT timestamp, btc_price, direction, our_confidence, score, action
        FROM realtime_predictions ORDER BY id DESC LIMIT ?
    """, (limit,)).fetchall()

    def normalize_dir(d):
        if isinstance(d, str):
            return "UP" if d.lower() == 'up' else "DN"
        return "UP" if d == 1 else "DN"

    return JSONResponse(content=sanitize([
        {"timestamp": r[0], "btc_price": r[1],
         "direction": normalize_dir(r[2]),
         "confidence": r[3], "score": r[4], "action": r[5],
        } for r in rows]))


@app.get("/api/sim_portfolio")
async def api_sim_portfolio():
    """Get latest sim trader portfolio snapshot — prefers COMBINED or most-active strategy."""
    conn = get_db()
    
    # Priority 1: latest COMBINED snapshot
    row = conn.execute("""
        SELECT timestamp, balance, total_value, total_pnl, total_return_pct,
               max_drawdown_pct, win_rate, wins, losses, total_trades, open_positions
        FROM sim_portfolio WHERE strategy='COMBINED'
        ORDER BY id DESC LIMIT 1
    """).fetchone()
    
    # Priority 2: latest snapshot from strategy with most total trades
    if not row:
        row = conn.execute("""
            SELECT timestamp, balance, total_value, total_pnl, total_return_pct,
                   max_drawdown_pct, win_rate, wins, losses, total_trades, open_positions
            FROM sim_portfolio
            WHERE strategy IN (
                SELECT strategy FROM sim_portfolio
                GROUP BY strategy
                ORDER BY MAX(total_trades) DESC
                LIMIT 1
            )
            ORDER BY id DESC LIMIT 1
        """).fetchone()
    
    # Priority 3: fallback to absolute latest
    if not row:
        row = conn.execute("""
            SELECT timestamp, balance, total_value, total_pnl, total_return_pct,
                   max_drawdown_pct, win_rate, wins, losses, total_trades, open_positions
            FROM sim_portfolio ORDER BY id DESC LIMIT 1
        """).fetchone()

    # Recent trades (from active strategies, excluding stale 'default' strategy)
    trades = conn.execute("""
        SELECT timestamp, direction, market_question, entry_price, amount,
               pnl, win, status, cycle, strategy
        FROM sim_trades 
        WHERE strategy != 'default' OR status = 'resolved'
        ORDER BY id DESC LIMIT 15
    """).fetchall()

    # Trade stats (all resolved trades)
    stats = conn.execute("""
        SELECT COUNT(*), SUM(CASE WHEN win=1 THEN 1 ELSE 0 END),
               SUM(CASE WHEN win=0 THEN 1 ELSE 0 END),
               AVG(pnl), SUM(pnl)
        FROM sim_trades WHERE status='resolved'
    """).fetchone()

    # Max/min balance from sim_portfolio (before closing conn)
    bal_stats = conn.execute("""
        SELECT MIN(balance), MAX(balance), MIN(total_value), MAX(total_value)
        FROM sim_portfolio
    """).fetchone()

    bal_stats = conn.execute("""
        SELECT MIN(balance), MAX(balance), MIN(total_value), MAX(total_value)
        FROM sim_portfolio
    """).fetchone()


    result = {"portfolio": None, "trades": [], "stats": None}

    if row:
        result["portfolio"] = {
            "timestamp": row[0], "balance": row[1], "total_value": row[2],
            "total_pnl": row[3], "total_return_pct": row[4],
            "max_drawdown_pct": row[5], "win_rate": row[6],
            "wins": row[7], "losses": row[8], "total_trades": row[9],
            "open_positions": row[10],
        }

    if trades:
        result["trades"] = [{
            "timestamp": t[0], "direction": t[1],
            "market_question": t[2][:50], "entry_price": t[3] or 0.5,
            "amount": t[4], "pnl": t[5], "win": bool(t[6]),
            "status": t[7], "cycle": t[8], "strategy": t[9] if len(t) > 9 else '',
            "odds": round(1.0 / t[3], 2) if t[3] and t[3] > 0 else 2.0,
        } for t in trades]

    if stats and stats[0]:
        result["stats"] = {
            "total_resolved": stats[0],
            "wins": stats[1],
            "losses": stats[2],
            "avg_pnl": stats[3],
            "total_pnl": stats[4],
        }

    if bal_stats and bal_stats[0] is not None:
        result["balance_range"] = {
            "min_balance": bal_stats[0],
            "max_balance": bal_stats[1],
            "min_value": bal_stats[2],
            "max_value": bal_stats[3],
        }

        # UP strategy stats
    up_stats = conn.execute("""
        SELECT COUNT(*), SUM(CASE WHEN win=1 THEN 1 ELSE 0 END)
        FROM sim_trades WHERE direction='Up' AND status='resolved'
    """).fetchone()
    if up_stats and up_stats[0] > 0:
        result["up_stats"] = {
            "total_trades": up_stats[0],
            "wins": up_stats[1],
            "win_rate": round(up_stats[1] / up_stats[0] * 100, 2),
        }

    return JSONResponse(content=sanitize(result))


@app.get("/api/sim_trades")
async def api_sim_trades(limit: int = 200, offset: int = 0):
    """Get all sim trades with pagination."""
    conn = get_db()
    
    # Total count
    total = conn.execute("""
        SELECT COUNT(*) FROM sim_trades WHERE strategy != 'default'
    """).fetchone()[0]
    
    # Paginated trades
    rows = conn.execute("""
        SELECT id, timestamp, direction, market_question, entry_price, amount,
               fee, pnl, win, status, wallet_balance_after, strategy
        FROM sim_trades
        WHERE strategy != 'default'
        ORDER BY id DESC
        LIMIT ? OFFSET ?
    """, (limit, offset)).fetchall()
    
    trades = []
    for r in rows:
        entry_price = r[4] if r[4] else 0.5
        odds = 1.0 / entry_price if entry_price > 0 else 2.0
        trades.append({
            "id": r[0],
            "timestamp": r[1],
            "direction": r[2],
            "market_question": r[3],
            "entry_price": entry_price,
            "amount": r[5],
            "fee": r[6],
            "pnl": r[7],
            "win": bool(r[8]),
            "status": r[9],
            "wallet_balance_after": r[10],
            "strategy": r[11],
            "odds": round(odds, 2),
        })
    
    return JSONResponse(content={"total": total, "trades": trades})


@app.get("/api/edges")
async def api_edges(limit: int = 15):
    conn = get_db()
    rows = conn.execute("""
        SELECT timestamp, market_question, market_up_price, market_down_price,
               our_direction, our_confidence, market_price, edge, edge_pct,
               ev_per_share, bet_on, action, recommendation, cycle
        FROM polymarket_edges ORDER BY id DESC LIMIT ?
    """, (limit,)).fetchall()

    return JSONResponse(content=sanitize([{
        "timestamp": r[0], "market_question": r[1],
        "market_up_price": r[2], "market_down_price": r[3],
        "our_direction": r[4], "our_confidence": r[5],
        "market_price": r[6], "edge": r[7], "edge_pct": r[8],
        "ev_per_share": r[9], "bet_on": r[10], "action": r[11],
        "recommendation": r[12], "cycle": r[13],
    } for r in rows]))


@app.get("/api/signals")
async def api_signals(limit: int = 10):
    conn = get_db()
    rows = conn.execute("""
        SELECT timestamp, direction, our_confidence, score, action, cycle, signals
        FROM realtime_predictions ORDER BY id DESC LIMIT ?
    """, (limit,)).fetchall()

    def normalize_dir(d):
        if isinstance(d, str):
            return "UP" if d.lower() == 'up' else "DN"
        return "UP" if d == 1 else "DN"

    return JSONResponse(content=sanitize([
        {"timestamp": r[0],
         "direction": normalize_dir(r[1]),
         "confidence": r[2], "score": r[3], "action": r[4],
         "cycle": r[5] if r[5] else 0,
         "signals": json.loads(r[6]) if r[6] else [],
        } for r in rows]))


def _html():
    return '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BTC 5-Min Predictor + Polymarket Edge</title>
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
.edge-positive { color: #00e676; }
.edge-negative { color: #ff5252; }
.edge-big { color: #e040fb; font-weight: 700; }
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
.signal-edge { background: rgba(224,64,251,0.15); color: #e040fb; }
.progress-bar { height: 8px; background: #1a2332; border-radius: 4px; overflow: hidden; margin-top: 8px; }
.progress-fill { height: 100%; border-radius: 4px; transition: width 0.3s; }
.status-dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 8px; }
.status-online { background: #00e676; box-shadow: 0 0 8px #00e676; }
.status-offline { background: #ff5252; }
.meta { color: #6b7a8d; font-size: 12px; }
.edge-card { background: linear-gradient(135deg, #1a1040, #111827); border-color: #6a1b9a; }
.action-badge { display: inline-block; padding: 3px 10px; border-radius: 6px; font-size: 13px; font-weight: 600; }
.action-buy { background: rgba(0,230,118,0.2); color: #00e676; }
.action-hold { background: rgba(255,215,64,0.2); color: #ffd740; }
.action-weak { background: rgba(0,212,255,0.2); color: #00d4ff; }
.filter-btn { padding: 4px 12px; background: #1a1040; color: #6b7a8d; border: 1px solid #2a1b5a; border-radius: 6px; cursor: pointer; font-size: 12px; transition: all 0.2s; }
.filter-btn:hover { background: #2a1b5a; color: #e040fb; }
.filter-btn.active { background: #2a1b5a; color: #e040fb; border-color: #e040fb; }
</style>
</head>
<body>
<div class="container">
<div class="header">
<h1>🔮 BTC 5-Min Predictor + Polymarket Edge</h1>
<div class="subtitle">WebSocket OKX | Order Flow + Vol + MR + Event | Edge Detection ≥3%</div>
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
<div class="card edge-card">
<h3>📊 Polymarket Edge</h3>
<div class="value" id="edge-value">--</div>
<div class="sub" id="edge-action">Loading...</div>
<div class="sub" id="edge-rec"></div>
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
<h3>📊 Stats</h3>
<div class="value" id="total-preds">0</div>
<div class="sub">UP: <span id="up-count">0</span> | DN: <span id="dn-count">0</span></div>
<div class="sub" id="edge-stats-sub"></div>
</div>
</div>

<div class="section">
<h2>📊 Polymarket Edge Analysis</h2>
<div id="edge-container"><p class="meta">Loading edge data...</p></div>
</div>

<div class="section">
<h2>🎰 Simulated Trading ($100 USDT, Max $1000/bet)</h2>
<div id="sim-container"><p class="meta">Loading simulation data...</p></div>
</div>

<div class="section">
<h2>📋 All Trades (<span id="trades-total">0</span>)</h2>
<div style="margin-bottom:12px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
<span class="meta">Filter:</span>
<button onclick="filterTrades('all')" id="filter-all" class="filter-btn active">All</button>
<button onclick="filterTrades('resolved')" id="filter-resolved" class="filter-btn">Resolved</button>
<button onclick="filterTrades('open')" id="filter-open" class="filter-btn">Open</button>
<button onclick="filterTrades('UP')" id="filter-UP" class="filter-btn">UP</button>
<span style="margin-left:auto;" class="meta" id="trades-range">Loading...</span>
</div>
<table>
<thead><tr><th>#</th><th>Time</th><th>Strategy</th><th>Direction</th><th>Amount</th><th>Price</th><th>Odds</th><th>P&L</th><th>Balance</th><th>Status</th></tr></thead>
<tbody id="trades-table"><tr><td colspan="10" class="meta">Loading...</td></tr></tbody>
</table>
<div style="margin-top:12px;display:flex;justify-content:center;gap:8px;">
<button onclick="loadTradesPage(-1)" id="trades-prev" style="padding:6px 16px;background:#1a1040;color:#e040fb;border:1px solid #6a1b9a;border-radius:6px;cursor:pointer;">← Prev</button>
<span class="meta" id="trades-page" style="padding:6px 12px;">Page 1</span>
<button onclick="loadTradesPage(1)" id="trades-next" style="padding:6px 16px;background:#1a1040;color:#e040fb;border:1px solid #6a1b9a;border-radius:6px;cursor:pointer;">Next →</button>
</div>
</div>

<div class="section">
<h2>📡 Live Signals</h2>
<div id="signals-container"><p class="meta">Loading...</p></div>
</div>

<div class="section">
<h2>📋 Recent Predictions</h2>
<table>
<thead><tr><th>Time</th><th>Price</th><th>Direction</th><th>Confidence</th><th>Score</th><th>Action</th></tr></thead>
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
            document.getElementById('total-preds').textContent = data.total_predictions || 0;
            document.getElementById('up-count').textContent = data.up_count || 0;
            document.getElementById('dn-count').textContent = data.dn_count || 0;

            if (data.edge_stats) {
                const es = data.edge_stats;
                document.getElementById('edge-stats-sub').textContent =
                    'Edges: ' + es.total_edges + ' | ≥3%: ' + es.edge_3pct_count + ' | Buys: ' + es.buy_signals;
            }

            if (data.latest) {
                const l = data.latest;
                const dirEl = document.getElementById('direction');
                const dirStr = typeof l.direction === 'string' ? l.direction : (l.direction === 1 ? 'UP' : 'DN');
                dirEl.textContent = dirStr === 'UP' || dirStr === 'Up' ? '⬆ UP' : '⬇ DN';
                dirEl.className = 'value ' + (dirStr === 'UP' || dirStr === 'Up' ? 'up' : 'dn');

                document.getElementById('price').textContent = '$' + (l.btc_price || 0).toLocaleString(undefined, {maximumFractionDigits:2});
                document.getElementById('spread').textContent = '$' + ((l.up_price || 0) - (l.down_price || 0)).toFixed(3);
                document.getElementById('confidence').textContent = ((l.confidence || 0.5) * 100).toFixed(1) + '%';

                const bar = document.getElementById('conf-bar');
                bar.style.width = ((l.confidence || 0.5) * 100) + '%';
                bar.style.background = (dirStr === 'UP' || dirStr === 'Up') ? '#00e676' : '#ff5252';

                const edge = l.edge || 0;
                const imbEl = document.getElementById('imbalance');
                imbEl.textContent = (edge > 0 ? '+' : '') + (edge * 100).toFixed(2) + '%';
                imbEl.className = 'value ' + (edge > 0.03 ? 'up' : edge < -0.03 ? 'dn' : 'neutral');

                document.getElementById('cvd').textContent = (l.score || 0).toFixed(4);
                document.getElementById('cvd').className = 'value ' + ((l.score || 0) > 0 ? 'up' : 'dn');
                document.getElementById('bs-ratio').textContent = (l.action || '--');

                if (l.timestamp !== lastCycle) {
                    lastCycle = l.timestamp;
                    updateEdge();
                    updateSim();
                    updateSignals();
                    updateTable();
                }
            }

            // Update edge card
            if (data.latest_edge) {
                const e = data.latest_edge;
                const edgeVal = document.getElementById('edge-value');
                const edgeAbs = Math.abs(e.edge);
                edgeVal.textContent = (e.edge > 0 ? '+' : '') + e.edge_pct;
                edgeVal.className = 'value ' + (edgeAbs >= 0.03 ? 'edge-big' : e.edge > 0 ? 'edge-positive' : 'edge-negative');

                const actionEl = document.getElementById('edge-action');
                const actionClass = e.action.startsWith('BUY') ? 'action-buy' : e.action === 'HOLD' ? 'action-hold' : 'action-weak';
                actionEl.innerHTML = '<span class="action-badge ' + actionClass + '">' + e.action + '</span> ' +
                    'Market: Up $' + (e.market_up_price || 0).toFixed(3) + ' / Down $' + (e.market_down_price || 0).toFixed(3);

                document.getElementById('edge-rec').textContent = e.recommendation || '';
            }

            const statusEl = document.getElementById('system-status');
            statusEl.innerHTML = '<p><span class="status-dot status-online"></span> OKX Connected</p>' +
                '<p class="meta">Avg Confidence: ' + ((data.avg_confidence || 0) * 100).toFixed(1) + '% | ' +
                'Avg Edge: ' + ((data.avg_edge || 0) * 100).toFixed(2) + '% | ' +
                'Avg OurConf: ' + ((data.avg_our_confidence || 0) * 100).toFixed(1) + '%</p>';
        })
        .catch(e => console.error('Status error:', e));
}

function updateEdge() {
    fetch(API + '/api/edges?limit=8')
        .then(r => r.json())
        .then(data => {
            const container = document.getElementById('edge-container');
            if (!data.length) { container.innerHTML = '<p class="meta">No edge data yet</p>'; return; }

            let html = '';
            data.forEach(item => {
                const edgeAbs = Math.abs(item.edge);
                const edgeClass = edgeAbs >= 0.03 ? 'edge-big' : item.edge > 0 ? 'edge-positive' : 'edge-negative';
                const actionClass = item.action.startsWith('BUY') ? 'action-buy' : item.action === 'HOLD' ? 'action-hold' : 'action-weak';

                html += '<div style="margin-bottom:12px;padding:12px;background:#0d1321;border-radius:8px;border-left:3px solid ' +
                    (item.edge >= 0.03 ? '#e040fb' : item.edge > 0 ? '#00e676' : '#ff5252') + ';">';
                html += '<div style="display:flex;justify-content:space-between;align-items:center;">';
                html += '<div>';
                html += '<span class="meta">' + formatTime(item.timestamp) + ' | Cycle #' + item.cycle + '</span><br>';
                html += '<span style="font-size:13px;color:#b0bec5;">' + (item.market_question || '').substring(0, 60) + '</span>';
                html += '</div>';
                html += '<div style="text-align:right;">';
                html += '<span class="action-badge ' + actionClass + '">' + item.action + '</span> ';
                html += '<span class="' + edgeClass + '" style="font-size:18px;font-weight:700;">' + item.edge_pct + '</span>';
                html += '</div></div>';

                html += '<div style="margin-top:8px;display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;font-size:13px;">';
                html += '<div>📊 Market: <span style="color:#00d4ff;">Up $' + (item.market_up_price || 0).toFixed(3) + '</span> / <span style="color:#ff5252;">Down $' + (item.market_down_price || 0).toFixed(3) + '</span></div>';
                html += '<div>🎯 Our: <span style="color:' + (item.our_direction === 'UP' ? '#00e676' : '#ff5252') + ';">' + item.our_direction + '</span> (' + ((item.our_confidence || 0.5) * 100).toFixed(1) + '%)</div>';
                html += '<div>💰 EV/Share: <span style="color:' + (item.ev_per_share >= 0 ? '#00e676' : '#ff5252') + ';">$' + (item.ev_per_share || 0).toFixed(4) + '</span></div>';
                html += '<div>🎲 Bet on: <span style="color:#ffd740;">' + (item.bet_on || '--') + '</span></div>';
                html += '</div>';

                html += '<div style="margin-top:6px;font-size:12px;color:#b0bec5;">💡 ' + (item.recommendation || '') + '</div>';
                html += '</div>';
            });
            container.innerHTML = html;
        });
}

function updateSim() {
    fetch(API + '/api/sim_portfolio')
        .then(r => r.json())
        .then(data => {
            const container = document.getElementById('sim-container');
            const p = data.portfolio;
            const trades = data.trades || [];
            const stats = data.stats;

            if (!p) {
                container.innerHTML = '<p class="meta">Waiting for simulation data...</p>';
                return;
            }

            let html = '';

            // Portfolio summary
            if (p) {
                const pnlColor = p.total_pnl >= 0 ? '#00e676' : '#ff5252';
                const returnColor = p.total_return_pct >= 0 ? '#00e676' : '#ff5252';
                html += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:16px;">';
                html += '<div style="padding:12px;background:#0d1321;border-radius:8px;text-align:center;">';
                html += '<div class="meta">Balance</div>';
                html += '<div style="font-size:22px;font-weight:700;">$' + p.balance.toFixed(2) + '</div></div>';
                html += '<div style="padding:12px;background:#0d1321;border-radius:8px;text-align:center;">';
                html += '<div class="meta">Total Value</div>';
                html += '<div style="font-size:22px;font-weight:700;">$' + p.total_value.toFixed(2) + '</div></div>';
                html += '<div style="padding:12px;background:#0d1321;border-radius:8px;text-align:center;">';
                html += '<div class="meta">P&L</div>';
                html += '<div style="font-size:22px;font-weight:700;color:' + pnlColor + ';">' + (p.total_pnl >= 0 ? '+' : '') + p.total_pnl.toFixed(2) + '</div></div>';
                html += '<div style="padding:12px;background:#0d1321;border-radius:8px;text-align:center;">';
                html += '<div class="meta">Return</div>';
                html += '<div style="font-size:22px;font-weight:700;color:' + returnColor + ';">' + p.total_return_pct.toFixed(2) + '%</div></div>';
                html += '<div style="padding:12px;background:#0d1321;border-radius:8px;text-align:center;">';
                html += '<div class="meta">Max Drawdown</div>';
                html += '<div style="font-size:22px;font-weight:700;color:#ff5252;">' + p.max_drawdown_pct.toFixed(2) + '%</div></div>';
                html += '<div style="padding:12px;background:#0d1321;border-radius:8px;text-align:center;">';
                html += '<div class="meta">Win Rate</div>';
                html += '<div style="font-size:22px;font-weight:700;color:#00e676;">' + p.win_rate.toFixed(1) + '%</div></div>';
                html += '<div style="padding:12px;background:#0d1321;border-radius:8px;text-align:center;">';
                html += '<div class="meta">Trades</div>';
                html += '<div style="font-size:22px;font-weight:700;">' + p.total_trades + '</div>';
                html += '<div style="font-size:12px;color:#6b7a8d;">' + p.wins + 'W / ' + p.losses + 'L</div></div>';
                html += '<div style="padding:12px;background:#0d1321;border-radius:8px;text-align:center;">';
                html += '<div class="meta">Open</div>';
                html += '<div style="font-size:22px;font-weight:700;color:#ffd740;">' + p.open_positions + '</div></div>';
                html += '</div>';

                // Balance range and strategy stats
                if (data.balance_range) {
                    html += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:16px;">';
                    html += '<div style="padding:12px;background:#0d1321;border-radius:8px;text-align:center;">';
                    html += '<div class="meta">Min Balance</div>';
                    html += '<div style="font-size:22px;font-weight:700;color:#ff5252;">$' + data.balance_range.min_balance.toFixed(2) + '</div></div>';
                    html += '<div style="padding:12px;background:#0d1321;border-radius:8px;text-align:center;">';
                    html += '<div class="meta">Max Balance</div>';
                    html += '<div style="font-size:22px;font-weight:700;color:#00e676;">$' + data.balance_range.max_balance.toFixed(2) + '</div></div>';
                    html += '</div>';
                }

                // UP strategy stats (UP-ONLY — no DOWN betting)
                if (data.up_stats) {
                    html += '<div style=\"display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:16px;\">';
                    const upColor = data.up_stats.win_rate >= 50 ? '#00e676' : '#ff5252';
                    html += '<div style=\"padding:12px;background:#0d1321;border-radius:8px;text-align:center;\">';
                    html += '<div class="meta">UP Strategy (UP-ONLY)</div>';
                    html += '<div style="font-size:22px;font-weight:700;color:' + upColor + ';">' + data.up_stats.win_rate.toFixed(1) + '%</div>';
                    html += '<div class="sub">' + data.up_stats.total_trades + ' trades</div></div>';
                    html += '</div>';
                }
            }

            if (!html) html = '<p class="meta">Waiting for simulation data...</p>';
            container.innerHTML = html;
        })
        .catch(e => console.error('Sim error:', e));
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
                html += '<td class="meta">' + formatTime(p.timestamp) + '</td>';
                html += '<td>$' + (p.btc_price || 0).toLocaleString(undefined, {maximumFractionDigits:0}) + '</td>';
                html += '<td class="' + dirClass + '"><strong>' + p.direction + '</strong></td>';
                html += '<td>' + ((p.confidence || 0.5) * 100).toFixed(1) + '%</td>';
                html += '<td>' + (p.score || 0).toFixed(4) + '</td>';
                html += '<td>' + (p.action || '--') + '</td>';
                html += '</tr>';
            });
            tbody.innerHTML = html;
        });
}

// Trades table state
let tradesPage = 0;
let tradesFilter = 'all';
const TRADES_PER_PAGE = 50;

function filterTrades(filter) {
    tradesFilter = filter;
    tradesPage = 0;
    // Update button states
    document.querySelectorAll('.filter-btn').forEach(btn => btn.classList.remove('active'));
    const btnId = 'filter-' + filter;
    const btn = document.getElementById(btnId);
    if (btn) btn.classList.add('active');
    loadTradesPage(0);
}

function loadTradesPage(direction) {
    if (direction === -1 && tradesPage > 0) tradesPage--;
    else if (direction === 1) tradesPage++;
    else if (direction === 0) tradesPage = 0;
    
    const offset = tradesPage * TRADES_PER_PAGE;
    let url = API + '/api/sim_trades?limit=' + TRADES_PER_PAGE + '&offset=' + offset;
    
    fetch(url)
        .then(r => r.json())
        .then(data => {
            const tbody = document.getElementById('trades-table');
            const totalEl = document.getElementById('trades-total');
            const rangeEl = document.getElementById('trades-range');
            const pageEl = document.getElementById('trades-page');
            
            totalEl.textContent = data.total;
            
            let trades = data.trades;
            
            // Apply filter
            if (tradesFilter === 'resolved') {
                trades = trades.filter(t => t.status === 'resolved');
            } else if (tradesFilter === 'open') {
                trades = trades.filter(t => t.status === 'open');
            } else if (tradesFilter === 'UP') {
                trades = trades.filter(t => t.strategy.toUpperCase() === 'UP' || t.direction === 'Up');
            }
            // UP-ONLY: DOWN filter removed
            
            const totalPages = Math.ceil(data.total / TRADES_PER_PAGE);
            pageEl.textContent = 'Page ' + (tradesPage + 1) + ' / ' + totalPages;
            
            const startNum = offset + 1;
            const endNum = Math.min(offset + trades.length, data.total);
            rangeEl.textContent = 'Showing ' + startNum + '-' + endNum + ' of ' + data.total;
            
            if (trades.length === 0) {
                tbody.innerHTML = '<tr><td colspan="10" class="meta">No trades found</td></tr>';
                return;
            }
            
            let html = '';
            trades.forEach(t => {
                const pnlColor = t.pnl >= 0 ? '#00e676' : '#ff5252';
                const statusColor = t.status === 'open' ? '#ffd740' : '#6b7a8d';
                const stratColor = t.strategy === 'UP' ? '#00e676' : '#ff5252';
                
                html += '<tr>';
                html += '<td class="meta">' + t.id + '</td>';
                html += '<td class="meta">' + formatTime(t.timestamp) + '</td>';
                html += '<td style="color:' + stratColor + ';font-weight:600;">' + t.strategy + '</td>';
                html += '<td style="color:' + (t.direction === 'Up' ? '#00e676' : '#ff5252') + ';">' + t.direction + '</td>';
                html += '<td>$' + t.amount.toFixed(2) + '</td>';
                html += '<td class="meta">$' + t.entry_price.toFixed(3) + '</td>';
                html += '<td style="color:#ffd740;font-weight:600;">' + (t.odds || (1/t.entry_price).toFixed(2)) + 'x</td>';
                html += '<td style="color:' + pnlColor + ';font-weight:600;">' + (t.pnl >= 0 ? '+' : '') + t.pnl.toFixed(2) + '</td>';
                html += '<td>$' + (t.wallet_balance_after || 0).toFixed(2) + '</td>';
                html += '<td style="color:' + statusColor + ';">' + t.status.toUpperCase() + '</td>';
                html += '</tr>';
            });
            tbody.innerHTML = html;
        })
        .catch(e => console.error('Trades error:', e));
}

update();
updateSim();
loadTradesPage(0);
setInterval(update, 5000);
setInterval(updateSim, 10000);
setInterval(function() { loadTradesPage(0); }, 30000);

</script>
</body>
</html>'''


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logger.info("Starting dashboard v3 on port 8765...")
    uvicorn.run(app, host="0.0.0.0", port=8765, log_level="warning")
