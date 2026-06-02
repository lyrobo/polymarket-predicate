"""Backtest: Compare original vs improved prediction algorithms"""

import json
import time
import numpy as np
import urllib.request
import ssl
from datetime import datetime, timezone, timedelta
from config import *
from technical_analysis import TechnicalAnalyzer
from prediction_engine import RuleBasedPredictor, MLPredictor
from improved_prediction_engine import ImprovedRuleBasedPredictor, ImprovedMLPredictor

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
    """Backtest both original and improved algorithms."""
    analyzer = TechnicalAnalyzer()
    
    # Original models
    orig_rule = RuleBasedPredictor()
    orig_ml = MLPredictor(n_estimators=50)
    
    # Improved models
    imp_rule = ImprovedRuleBasedPredictor()
    imp_ml = ImprovedMLPredictor(n_estimators=100)
    
    print(f"\n{'='*70}")
    print(f"  BTC 5-Minute Prediction Backtest - Original vs Improved")
    print(f"{'='*70}")
    print(f"  Total candles: {len(klines)}")
    print(f"  Range: {datetime.fromtimestamp(klines[0]['timestamp'], tz=timezone.utc).strftime('%m-%d %H:%M')} → {datetime.fromtimestamp(klines[-1]['timestamp'], tz=timezone.utc).strftime('%m-%d %H:%M')}")
    print(f"  Price: ${min(k['close'] for k in klines):,.2f} → ${max(k['close'] for k in klines):,.2f}")
    print(f"  Window: {window} min | Train: {train_ratio*100:.0f}%")
    print(f"{'='*70}\n")
    
    # Phase 1: Training
    train_end = int(len(klines) * train_ratio)
    print(f"Phase 1: Training (candles 0-{train_end})...")
    
    train_features_orig, train_labels_orig = [], []
    train_features_imp, train_labels_imp = [], []
    
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
            train_features_orig.append(feature_vec)
            train_labels_orig.append(actual)
            train_features_imp.append(feature_vec)
            train_labels_imp.append(actual)
    
    if train_features_orig:
        orig_ml.train(np.array(train_features_orig), np.array(train_labels_orig))
        imp_ml.train(np.array(train_features_imp), np.array(train_labels_imp))
        print(f"  Trained on {len(train_features_orig)} samples")
        print(f"  UP: {sum(l==1 for l in train_labels_orig)}  DOWN: {sum(l==-1 for l in train_labels_orig)}")
    
    # Phase 2: Testing
    print(f"\nPhase 2: Testing (candles {train_end}-{len(klines)})...")
    
    orig_rule_correct = orig_ml_correct = orig_ens_correct = 0
    imp_rule_correct = imp_ml_correct = imp_ens_correct = 0
    total = 0
    
    predictions = []
    
    for i in range(train_end, len(klines) - window):
        window_data = klines[max(0, i-30):i]
        if len(window_data) < 30:
            continue
        
        indicators = analyzer.compute_all(window_data)
        if not indicators or "latest" not in indicators:
            continue
        
        # Original predictions
        orig_rule_pred = orig_rule.predict(indicators)
        feature_vec = analyzer.get_feature_vector(indicators)
        orig_ml_pred = orig_ml.predict(feature_vec) if len(feature_vec) > 0 else {"direction": 0, "confidence": 0.5}
        
        orig_rule_conf = orig_rule_pred["confidence"]
        orig_ml_conf = orig_ml_pred["confidence"]
        orig_combined = 0.7 * orig_rule_conf + (0.3 * orig_ml_conf if orig_ml_pred["direction"] != 0 else 0)
        if orig_ml_pred["direction"] == 0:
            orig_combined = orig_rule_conf
        
        orig_ens_dir = 1 if orig_combined > 0.5 else -1
        orig_rule_dir = orig_rule_pred["direction"]
        orig_ml_dir = orig_ml_pred["direction"] if orig_ml_pred["direction"] != 0 else orig_ens_dir
        
        # Improved predictions
        imp_rule_pred = imp_rule.predict(indicators)
        imp_ml_pred = imp_ml.predict(feature_vec) if len(feature_vec) > 0 else {"direction": 0, "confidence": 0.5}
        
        imp_rule_conf = imp_rule_pred["confidence"]
        imp_ml_conf = imp_ml_pred["confidence"]
        
        # Adaptive weighting
        if imp_rule_pred["direction"] == imp_ml_pred["direction"] and imp_ml_pred["direction"] != 0:
            imp_w_rule, imp_w_ml = 0.6, 0.4
        else:
            imp_w_rule, imp_w_ml = 0.75, 0.25
        
        imp_combined = imp_w_rule * imp_rule_conf + (imp_w_ml * imp_ml_conf if imp_ml_pred["direction"] != 0 else 0)
        if imp_ml_pred["direction"] == 0:
            imp_combined = imp_rule_conf
        
        imp_ens_dir = 1 if imp_combined > 0.5 else -1
        imp_rule_dir = imp_rule_pred["direction"]
        imp_ml_dir = imp_ml_pred["direction"] if imp_ml_pred["direction"] != 0 else imp_ens_dir
        
        # Actual outcome
        future_idx = min(i + window, len(klines) - 1)
        actual = 1 if klines[future_idx]["close"] >= klines[i]["close"] else -1
        
        # Count correct
        if orig_rule_dir == actual: orig_rule_correct += 1
        if orig_ml_dir == actual: orig_ml_correct += 1
        if orig_ens_dir == actual: orig_ens_correct += 1
        
        if imp_rule_dir == actual: imp_rule_correct += 1
        if imp_ml_dir == actual: imp_ml_correct += 1
        if imp_ens_dir == actual: imp_ens_correct += 1
        
        total += 1
        
        predictions.append({
            "ts": klines[i]["timestamp"],
            "price": klines[i]["close"],
            "orig_conf": orig_combined,
            "imp_conf": imp_combined,
            "orig_pred": orig_ens_dir,
            "imp_pred": imp_ens_dir,
            "actual": actual,
            "orig_hit": orig_ens_dir == actual,
            "imp_hit": imp_ens_dir == actual,
        })
    
    if total == 0:
        print("  No test samples")
        return None
    
    # Results comparison
    print(f"\n{'='*70}")
    print(f"  Results Comparison ({total} predictions)")
    print(f"{'='*70}")
    print(f"  {'Model':<20} {'Accuracy':>10} {'Correct':>10} {'Edge':>8}")
    print(f"  {'-'*50}")
    
    print(f"  {'Original Rule':<20} {orig_rule_correct/total*100:>9.1f}% {orig_rule_correct:>10} {orig_rule_correct/total*100-50:>+7.1f}%")
    print(f"  {'Original ML':<20} {orig_ml_correct/total*100:>9.1f}% {orig_ml_correct:>10} {orig_ml_correct/total*100-50:>+7.1f}%")
    print(f"  {'Original Combined':<20} {orig_ens_correct/total*100:>9.1f}% {orig_ens_correct:>10} {orig_ens_correct/total*100-50:>+7.1f}%")
    print(f"  {'Improved Rule':<20} {imp_rule_correct/total*100:>9.1f}% {imp_rule_correct:>10} {imp_rule_correct/total*100-50:>+7.1f}%")
    print(f"  {'Improved ML':<20} {imp_ml_correct/total*100:>9.1f}% {imp_ml_correct:>10} {imp_ml_correct/total*100-50:>+7.1f}%")
    print(f"  {'Improved Combined':<20} {imp_ens_correct/total*100:>9.1f}% {imp_ens_correct:>10} {imp_ens_correct/total*100-50:>+7.1f}%")
    print(f"  {'Random baseline':<20} {50.0:>9.1f}% {'-':>10} {'0.0%':>7}")
    
    print(f"\n{'='*70}")
    
    # Confidence distribution
    print(f"\n  Improved Confidence Distribution:")
    bins = {"50-55%": [0,0], "55-60%": [0,0], "60-65%": [0,0], "65-70%": [0,0], "70%+": [0,0]}
    for p in predictions:
        c = p["imp_conf"]
        if c < 0.55: k = "50-55%"
        elif c < 0.60: k = "55-60%"
        elif c < 0.65: k = "60-65%"
        elif c < 0.70: k = "65-70%"
        else: k = "70%+"
        bins[k][0] += 1
        if p["imp_hit"]: bins[k][1] += 1
    
    print(f"  {'Range':<10} {'Count':>6} {'Correct':>8} {'Accuracy':>10}")
    print(f"  {'-'*38}")
    for k, (t, c) in bins.items():
        if t > 0:
            print(f"  {k:<10} {t:>6} {c:>8} {c/t*100:>9.1f}%")
    
    # Recent sample
    print(f"\n  Last 10 predictions (Improved):")
    print(f"  {'Time':>16} {'Price':>10} {'Conf':>6} {'Pred':>5} {'Real':>5} {'Hit':>4}")
    print(f"  {'-'*50}")
    for p in predictions[-10:]:
        ts = datetime.fromtimestamp(p["ts"], tz=timezone.utc).strftime("%m-%d %H:%M")
        pd = "UP" if p["imp_pred"]==1 else "DN"
        ad = "UP" if p["actual"]==1 else "DN"
        h = "✅" if p["imp_hit"] else "❌"
        print(f"  {ts:>16} ${p['price']:>8,.0f} {p['imp_conf']*100:>5.0f}% {pd:>5} {ad:>5} {h:>4}")
    
    # Save
    path = DATA_DIR / "improved_backtest_results.json"
    with open(path, "w") as f:
        json.dump({
            "total": total,
            "orig_rule": round(orig_rule_correct/total, 4),
            "orig_ml": round(orig_ml_correct/total, 4),
            "orig_ens": round(orig_ens_correct/total, 4),
            "imp_rule": round(imp_rule_correct/total, 4),
            "imp_ml": round(imp_ml_correct/total, 4),
            "imp_ens": round(imp_ens_correct/total, 4),
            "predictions": predictions[-100:],
        }, f, indent=2)
    print(f"\n  Saved: {path}")
    
    return {
        "total": total,
        "orig_ens": orig_ens_correct/total,
        "imp_ens": imp_ens_correct/total,
    }


if __name__ == "__main__":
    klines = fetch_historical_klines(days=3)
    if len(klines) >= 100:
        results = run_backtest(klines, window=5, train_ratio=0.7)
        if results:
            print(f"\n{'='*70}")
            if results["imp_ens"] > results["orig_ens"]:
                print(f"  ✅ Improved algorithm better: {results['imp_ens']*100:.1f}% vs {results['orig_ens']*100:.1f}%")
            else:
                print(f"  ⚠️  Original still better: {results['orig_ens']*100:.1f}% vs {results['imp_ens']*100:.1f}%")
            print(f"{'='*70}")
    else:
        print(f"❌ Insufficient data: {len(klines)} candles")
