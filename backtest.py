"""Backtest: Validate BTC prediction algorithm on historical data"""

import json
import time
import numpy as np
import urllib.request
import ssl
from datetime import datetime, timezone, timedelta
from config import *
from technical_analysis import TechnicalAnalyzer
from prediction_engine import RuleBasedPredictor, MLPredictor

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
    """Backtest prediction algorithm."""
    analyzer = TechnicalAnalyzer()
    rule_predictor = RuleBasedPredictor()
    ml_predictor = MLPredictor(n_estimators=50)
    
    results = []
    train_features, train_labels = [], []
    
    print(f"\n{'='*60}")
    print(f"  BTC 5-Minute Prediction Backtest")
    print(f"{'='*60}")
    print(f"  Total candles: {len(klines)}")
    print(f"  Range: {datetime.fromtimestamp(klines[0]['timestamp'], tz=timezone.utc).strftime('%m-%d %H:%M')} → {datetime.fromtimestamp(klines[-1]['timestamp'], tz=timezone.utc).strftime('%m-%d %H:%M')}")
    print(f"  Price: ${min(k['close'] for k in klines):,.2f} → ${max(k['close'] for k in klines):,.2f}")
    print(f"  Window: {window} min | Train: {train_ratio*100:.0f}%")
    print(f"{'='*60}\n")
    
    # Phase 1: Training
    train_end = int(len(klines) * train_ratio)
    print(f"Phase 1: Training (candles 0-{train_end})...")
    
    for i in range(30, train_end):
        window_data = klines[max(0, i-30):i]
        if len(window_data) < 30:
            continue
        
        indicators = analyzer.compute_all(window_data)
        if not indicators or "latest" not in indicators:
            continue
        
        future_idx = min(i + window, len(klines) - 1)
        actual = 1 if klines[future_idx]["close"] >= klines[i]["close"] else -1
        
        feature_vec = analyzer.get_feature_vector(indicators)
        if len(feature_vec) > 0:
            train_features.append(feature_vec)
            train_labels.append(actual)
    
    if train_features:
        ml_predictor.train(np.array(train_features), np.array(train_labels))
        print(f"  Trained on {len(train_features)} samples")
        print(f"  UP: {sum(l==1 for l in train_labels)}  DOWN: {sum(l==-1 for l in train_labels)}")
    
    # Phase 2: Testing
    print(f"\nPhase 2: Testing (candles {train_end}-{len(klines)})...")
    
    rule_correct = ml_correct = ensemble_correct = 0
    high_conf_correct = high_conf_total = 0
    total = 0
    predictions = []
    
    for i in range(train_end, len(klines) - window):
        window_data = klines[max(0, i-30):i]
        if len(window_data) < 30:
            continue
        
        indicators = analyzer.compute_all(window_data)
        if not indicators or "latest" not in indicators:
            continue
        
        rule_pred = rule_predictor.predict(indicators)
        feature_vec = analyzer.get_feature_vector(indicators)
        ml_pred = ml_predictor.predict(feature_vec) if len(feature_vec) > 0 else {"direction": 0, "confidence": 0.5}
        
        rule_conf = rule_pred["confidence"]
        ml_conf = ml_pred["confidence"]
        combined = 0.7 * rule_conf + (0.3 * ml_conf if ml_pred["direction"] != 0 else 0)
        if ml_pred["direction"] == 0:
            combined = rule_conf
        
        ensemble_dir = 1 if combined > 0.5 else -1
        rule_dir = rule_pred["direction"]
        ml_dir = ml_pred["direction"] if ml_pred["direction"] != 0 else ensemble_dir
        
        future_idx = min(i + window, len(klines) - 1)
        actual = 1 if klines[future_idx]["close"] >= klines[i]["close"] else -1
        
        if rule_dir == actual: rule_correct += 1
        if ml_dir == actual: ml_correct += 1
        if ensemble_dir == actual: ensemble_correct += 1
        total += 1
        
        if combined > 0.60:
            high_conf_total += 1
            if ensemble_dir == actual: high_conf_correct += 1
        
        predictions.append({
            "ts": klines[i]["timestamp"],
            "price": klines[i]["close"],
            "conf": combined,
            "pred": ensemble_dir,
            "actual": actual,
            "hit": ensemble_dir == actual,
            "high_conf": combined > 0.60,
        })
    
    if total == 0:
        print("  No test samples")
        return None
    
    # Results
    print(f"\n{'='*60}")
    print(f"  Backtest Results ({total} predictions)")
    print(f"{'='*60}")
    print(f"  Rule-based:     {rule_correct/total*100:.1f}% ({rule_correct}/{total})")
    print(f"  ML ensemble:    {ml_correct/total*100:.1f}% ({ml_correct}/{total})")
    print(f"  Combined:       {ensemble_correct/total*100:.1f}% ({ensemble_correct}/{total})")
    print(f"  Random baseline: 50.0%")
    print(f"  Edge:            +{ensemble_correct/total*100 - 50:.1f}%")
    print(f"")
    print(f"  High conf (>60%): {high_conf_correct}/{high_conf_total} = {high_conf_correct/max(high_conf_total,1)*100:.1f}%")
    print(f"{'='*60}")
    
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
    
    # Recent sample
    print(f"\n  Last 15 predictions:")
    print(f"  {'Time':>16} {'Price':>10} {'Conf':>6} {'Pred':>5} {'Real':>5} {'Hit':>4}")
    print(f"  {'-'*50}")
    for p in predictions[-15:]:
        ts = datetime.fromtimestamp(p["ts"], tz=timezone.utc).strftime("%m-%d %H:%M")
        pd = "UP" if p["pred"]==1 else "DN"
        ad = "UP" if p["actual"]==1 else "DN"
        h = "✅" if p["hit"] else "❌"
        print(f"  {ts:>16} ${p['price']:>8,.0f} {p['conf']*100:>5.0f}% {pd:>5} {ad:>5} {h:>4}")
    
    # Save
    path = DATA_DIR / "backtest_results.json"
    with open(path, "w") as f:
        json.dump({
            "total": total,
            "rule_acc": round(rule_correct/total, 4),
            "ml_acc": round(ml_correct/total, 4),
            "ens_acc": round(ensemble_correct/total, 4),
            "bins": {k: {"total": v[0], "correct": v[1]} for k, v in bins.items()},
            "predictions": predictions[-100:],
        }, f, indent=2)
    print(f"\n  Saved: {path}")
    
    return {
        "total": total,
        "rule": rule_correct/total,
        "ml": ml_correct/total,
        "ensemble": ensemble_correct/total,
    }


if __name__ == "__main__":
    klines = fetch_historical_klines(days=3)
    if len(klines) >= 100:
        results = run_backtest(klines, window=5, train_ratio=0.7)
        if results:
            print(f"\n{'='*60}")
            if results["ensemble"] > 0.52:
                print(f"  ✅ Algorithm shows predictive power ({results['ensemble']*100:.1f}%)")
            elif results["ensemble"] > 0.50:
                print(f"  ⚠️  Slight edge, needs tuning ({results['ensemble']*100:.1f}%)")
            else:
                print(f"  ❌ No predictive power ({results['ensemble']*100:.1f}%)")
            print(f"{'='*60}")
    else:
        print(f"❌ Insufficient data: {len(klines)} candles")
