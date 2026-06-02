import sys
sys.path.insert(0, "/opt/btc-polymarket-predictor")
from unified_strategy import UnifiedStrategyEngine

engine = UnifiedStrategyEngine()

for i in range(10):
    result = engine.predict()
    direction = result['direction']
    confidence = result['confidence']
    score = result['score']
    modules = result['modules']
    print(f"Prediction {i+1}: {'UP' if direction == 1 else 'DN'} ({confidence:.2%})")
    print(f"  Score: {score:.4f}")
    print(f"  Modules: OF={modules['order_flow']:.2%}, Vol={modules['volatility']:.2%}, MR={modules['mean_reversion']:.2%}, Event={modules['event_driven']:.2%}")
    print()
