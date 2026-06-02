"""Backtest: Validate unified strategy on historical data"""

import json
import time
import numpy as np
import urllib.request
import ssl
from datetime import datetime, timezone
from config import *
from technical_analysis import TechnicalAnalyzer
from unified_strategy import UnifiedStrategyEngine

ctx = ssl._create_unverified_context()


def fetch_historical_klines(days=3, interval="1m"):
    """Fetch historical 1-minute klines from Binance."""
    all_klines = []
    end_time = int(time.time() * 1000)
    limit = 1000
    
    print(f"Fetching {days} days of 1-min klines...")
    
    for page in range(15):
        url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={interval}&limit={limit}&endTime={end_time}"
        req = urllib.request.Request(url, headers={"User-Agent": "BTC-Predictor/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                data = json.loads(resp.read().decode())
                if not data:
                    break
                
                klines = [{
                    "timestamp": k[0] / 1000,
                    "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                    "close": float(k[4]), "volume": float(k[5]),
                    "close_time": k[6] / 1000, "quote_volume": float(k[7]),
                    "trades": int(k[8]),
                } for k in data]
                
                all_klines.extend(klines)
                end_time = data[0][0]
                print(f"  Page {page+1}: {len(data)} candles (total: {len(all_klines)})")
                
                if len(data) < limit:
                    break
                time.sleep(0.3)
        except Exception as e:
            print(f"  Error page {page}: {e}")
            break
    
    return all_klines


def run_backtest(klines, window=5, train_ratio=0.7):
    """Backtest unified strategy."""
    engine = UnifiedStrategyEngine()
    analyzer = TechnicalAnalyzer()
    
    print(f"\n{'='*70}")
    print(f"  Unified Strategy Backtest")
    print(f"{'='*70}")
    print(f"  Total candles: {len(klines)}")
    print(f"  Range: {datetime.fromtimestamp(klines[0]['timestamp'], tz=timezone.utc).strftime('%m-%d %H:%M')} → {datetime.fromtimestamp(klines[-1]['timestamp'], tz=timezone.utc).strftime('%m-%d %H:%M')}")
    print(f"  Price: ${min(k['close'] for k in klines):,.2f} → ${max(k['close'] for k in klines):,.2f}")
    print(f"  Window: {window} min | Train: {train_ratio*100:.0f}%")
    print(f"{'='*70}\n")
    
    # Phase 1: Training (warm up the engine)
    train_end = int(len(klines) * train_ratio)
    print(f"Phase 1: Warming up engine (candles 0-{train_end})...")
    
    for i in range(30, train_end, 10):  # Every 10 candles
        window_data = klines[max(0, i-30):i]
        if len(window_data) < 30:
            continue
        
        indicators = analyzer.compute_all(window_data)
        if indicators:
            engine.predict(klines=window_data)
    
    print(f"  Engine warmed up with {train_end} samples")
    
    # Phase 2: Testing
    print(f"\nPhase 2: Testing (candles {train_end}-{len(klines)})...")
    
    correct = total = 0
    high_conf_correct = high_conf_total = 0
    predictions = []
    
    for i in range(train_end, len(klines) - window):
        window_data = klines[max(0, i-30):i]
        if len(window_data) < 30:
            continue
        
        # Get prediction
        result = engine.predict(klines=window_data)
        
        # Determine actual outcome
        future_idx = min(i + window, len(klines) - 1)
        actual = 1 if klines[future_idx]["close"] >= klines[i]["close"] else -1
        
        # Check if prediction matches
        pred = result["direction"]
        if pred == actual:
            correct += 1
        total += 1
        
        # High confidence
        if result["confidence"] > 0.60:
            high_conf_total += 1
            if pred == actual:
                high_conf_correct += 1
        
        predictions.append({
            "ts": klines[i]["timestamp"],
            "price": klines[i]["close"],
            "conf": result["confidence"],
            "pred": pred,
            "actual": actual,
            "hit": pred == actual,
            "high_conf": result["confidence"] > 0.60,
            "action": result["action"],
        })
    
    if total == 0:
        print("  No test samples")
        return None
    
    # Results
    print(f"\n{'='*70}")
    print(f"  Results ({total} predictions)")
    print(f"{'='*70}")
    print(f"  Overall Accuracy:     {correct/total*100:.1f}% ({correct}/{total})")
    print(f"  Random baseline:      50.0%")
    print(f"  Edge:                 +{correct/total*100 - 50:.1f}%")
    print(f"")
    print(f"  High conf (>60%):     {high_conf_correct}/{high_conf_total} = {high_conf_correct/max(high_conf_total,1)*100:.1f}%")
    print(f"{'='*70}")
    
    # Confidence bins
    bins = {"50-55%": [0,0], "55-60%": [0,0], "60-65%": [0,0], "65-70%": [0,0], "70%+": [0,0]}
    for p in predictions:
        c = p["conf"]
        if c < 0.55: k = "50-55%"
        elif c < 0.60: k = "55-60%"
        elif c < 0.65: k = "60-65%"
        elif c < 0.70: k = "65-70%"
        else: k = "70%+"
        bins[k][0] += 1
        if p["hit"]: bins[k][1] += 1
    
    print(f"\n  Confidence Distribution:")
    print(f"  {'Range':<10} {'Count':>6} {'Correct':>8} {'Accuracy':>10}")
    print(f"  {'-'*38}")
    for k, (t, c) in bins.items():
        if t > 0:
            print(f"  {k:<10} {t:>6} {c:>8} {c/t*100:>9.1f}%")
    
    # Action distribution
    actions = {}
    for p in predictions:
        a = p["action"]
        if a not in actions:
            actions[a] = [0, 0]
        actions[a][0] += 1
        if p["hit"]:
            actions[a][1] += 1
    
    print(f"\n  Action Distribution:")
    print(f"  {'Action':<15} {'Count':>6} {'Correct':>8} {'Accuracy':>10}")
    print(f"  {'-'*43}")
    for a, (t, c) in sorted(actions.items()):
        print(f"  {a:<15} {t:>6} {c:>8} {c/t*100:>9.1f}%")
    
    # Recent sample
    print(f"\n  Last 15 predictions:")
    print(f"  {'Time':>16} {'Price':>10} {'Conf':>6} {'Pred':>5} {'Real':>5} {'Hit':>4} {'Action':<12}")
    print(f"  {'-'*62}")
    for p in predictions[-15:]:
        ts = datetime.fromtimestamp(p["ts"], tz=timezone.utc).strftime("%m-%d %H:%M")
        pd = "UP" if p["pred"]==1 else "DN"
        ad = "UP" if p["actual"]==1 else "DN"
        h = "✅" if p["hit"] else "❌"
        print(f"  {ts:>16} ${p['price']:>8,.0f} {p['conf']*100:>5.0f}% {pd:>5} {ad:>5} {h:>4} {p['action']:<12}")
    
    # Save
    path = DATA_DIR / "unified_backtest_results.json"
    with open(path, "w") as f:
        json.dump({
            "total": total,
            "accuracy": round(correct/total, 4),
            "high_conf_accuracy": round(high_conf_correct/max(high_conf_total,1), 4),
            "bins": {k: {"total": v[0], "correct": v[1]} for k, v in bins.items()},
            "actions": {k: {"total": v[0], "correct": v[1]} for k, v in actions.items()},
            "predictions": predictions[-100:],
        }, f, indent=2)
    print(f"\n  Saved: {path}")
    
    return {
        "total": total,
        "accuracy": correct/total,
        "high_conf_accuracy": high_conf_correct/max(high_conf_total,1),
    }


if __name__ == "__main__":
    klines = fetch_historical_klines(days=3)
    if len(klines) >= 100:
        results = run_backtest(klines, window=5, train_ratio=0.7)
        if results:
            print(f"\n{'='*70}")
            if results["accuracy"] > 0.52:
                print(f"  ✅ Strategy shows predictive power ({results['accuracy']*100:.1f}%)")
            elif results["accuracy"] > 0.50:
                print(f"  ⚠️  Slight edge, needs tuning ({results['accuracy']*100:.1f}%)")
            else:
                print(f"  ❌ No predictive power ({results['accuracy']*100:.1f}%)")
            print(f"{'='*70}")
    else:
        print(f"❌ Insufficient data: {len(klines)} candles")
