import sys
sys.path.insert(0, "/opt/btc-polymarket-predictor")
from unified_strategy import UnifiedStrategyEngine
engine = UnifiedStrategyEngine()
# Mock prediction
result = engine.predict()
print(f"Direction: {result['direction']}")
print(f"Confidence: {result['confidence']}")
print(f"Score: {result['score']}")
print(f"Action: {result['action']}")
print(f"Modules: {result['modules']}")
