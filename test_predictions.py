import sys
sys.path.insert(0, "/opt/btc-polymarket-predictor")
from unified_strategy import UnifiedStrategyEngine
import random

engine = UnifiedStrategyEngine()

up_count = 0
dn_count = 0
confidences = []

for i in range(20):
    result = engine.predict()
    direction = result['direction']
    confidence = result['confidence']
    confidences.append(confidence)
    if direction == 1:
        up_count += 1
    else:
        dn_count += 1
    print(f"Prediction {i+1}: {'UP' if direction == 1 else 'DN'} ({confidence:.2%}), score={result['score']:.4f}")

print(f"\nTotal: UP={up_count}, DN={dn_count}")
print(f"Average confidence: {sum(confidences)/len(confidences):.2%}")
print(f"Min confidence: {min(confidences):.2%}")
print(f"Max confidence: {max(confidences):.2%}")
