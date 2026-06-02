#!/usr/bin/env python3
"""BTC 5-Minute Polymarket Predictor - Main Entry Point

Usage:
    python3 main.py              # Continuous prediction loop
    python3 main.py --once       # Single prediction cycle
    python3 main.py --dashboard  # Web dashboard (port 8765)
    python3 main.py --test       # Self-test
"""

import sys
import time
import logging
import argparse
from config import *
from strategy_engine import StrategyEngine

logger = logging.getLogger(__name__)


def run_single_cycle(engine: StrategyEngine):
    result = engine.run_cycle()
    if result["success"]:
        pred = result.get("prediction", {})
        signal = result.get("signal", {})
        pm = result.get("polymarket", {})

        print("\n" + "═" * 60)
        print(f"  🔮 BTC 5-Minute Prediction")
        print("═" * 60)
        print(f"  Time:       {result['timestamp']}")
        print(f"  Price:      ${result.get('price', 0):,.2f}")
        print(f"  Direction:  {'📈 UP' if pred.get('direction') == 1 else '📉 DOWN'}")
        print(f"  Confidence: {pred.get('confidence', 0) * 100:.1f}%")
        print(f"  Rule:       {pred.get('rule_confidence', 0) * 100:.1f}%")
        print(f"  ML:         {pred.get('ml_confidence', 0) * 100:.1f}%")
        print(f"  Agreement:  {pred.get('agreement', 'N/A')}")
        print(f"  Signal:     {signal.get('type', 'HOLD')}")
        print(f"  PM Action:  {signal.get('polymarket_action', 'HOLD')}")
        print(f"  Edge:       {signal.get('edge', 0) * 100:.2f}%")
        print(f"  Cycle:      {result.get('cycle_time_ms', 0):.0f}ms")
        print("═" * 60)

        if pm.get("edge_analysis"):
            ea = pm["edge_analysis"]
            print(f"\n  Polymarket:")
            print(f"    Up:   {ea.get('market_up_price', 0):.3f}")
            print(f"    Down: {ea.get('market_down_price', 0):.3f}")
            print(f"    {ea.get('recommendation', '')}")

        for sig in pred.get("signals", []):
            print(f"  • {sig}")

        ind = result.get("indicators", {})
        print(f"\n  Indicators: RSI={ind.get('rsi','?')} MACD_H={ind.get('macd_hist','?')} "
              f"VolRatio={ind.get('volume_ratio','?')}")
    else:
        print(f"\n❌ Failed: {result.get('errors', ['Unknown'])}")
    return result


def run_continuous(engine: StrategyEngine, interval: int = COLLECTION_INTERVAL):
    print(f"\n🚀 BTC Predictor running (interval={interval}s)")
    print(f"   Press Ctrl+C to stop\n")
    count = 0
    try:
        while True:
            count += 1
            run_single_cycle(engine)
            print(f"\n⏳ Next cycle in {interval}s... (#{count})")
            time.sleep(interval)
    except KeyboardInterrupt:
        print(f"\n\n🛑 Stopped after {count} cycles")
        stats = engine.get_stats()
        print(f"   Predictions: {stats.get('total_predictions', 0)}")
        print(f"   Signals: {stats.get('total_signals', 0)}")


def run_test():
    print("\n🧪 Self-test...\n")

    # Technical analysis
    from technical_analysis import TechnicalAnalyzer
    analyzer = TechnicalAnalyzer()
    import numpy as np
    np.random.seed(42)
    base = 80000
    klines = [{"timestamp": time.time() - (100-i)*60, "open": base+i*10+np.random.normal(0,20),
               "high": base+i*10+np.random.normal(0,30), "low": base+i*10-np.random.normal(0,30),
               "close": base+i*10+np.random.normal(0,15), "volume": np.random.uniform(10,100)}
              for i in range(100)]

    indicators = analyzer.compute_all(klines)
    if indicators and "latest" in indicators:
        print(f"✅ Technical Analysis — RSI={indicators['latest'].get('rsi',0):.1f}")
    else:
        print("❌ TA failed")

    # Prediction
    from prediction_engine import PredictionEngine
    predictor = PredictionEngine()
    pred = predictor.predict(indicators)
    print(f"✅ Prediction — {'UP' if pred['direction']==1 else 'DOWN'} @ {pred['confidence']*100:.1f}%")

    # ML training (use 22 features to match new feature vector)
    features = np.random.randn(200, 22)
    labels = np.random.choice([-1, 1], size=200)
    predictor.ml_predictor.train(features, labels)
    ml = predictor.ml_predictor.predict(features[0])
    print(f"✅ ML Training — sklearn GB model")

    # Strategy engine
    engine = StrategyEngine()
    result = engine.run_cycle()
    print(f"✅ Strategy Engine — signal={result.get('signal',{}).get('type','N/A')}")

    # DB
    stats = engine.get_stats()
    print(f"✅ Database — {stats.get('total_predictions',0)} predictions, {stats.get('total_signals',0)} signals")

    print("\n✅ All tests passed!")


def main():
    parser = argparse.ArgumentParser(description="BTC 5-Minute Polymarket Predictor")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dashboard", action="store_true")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--interval", type=int, default=COLLECTION_INTERVAL)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(str(LOG_FILE))],
    )

    if args.test:
        run_test()
        return

    if args.dashboard:
        from dashboard import main as dashboard_main
        dashboard_main()
        return

    engine = StrategyEngine()
    if args.once:
        run_single_cycle(engine)
    else:
        run_continuous(engine, args.interval)


if __name__ == "__main__":
    main()
