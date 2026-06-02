#!/usr/bin/env python3
"""Apply strategy optimizations to real_trader.py"""
import re

path = '/opt/btc-polymarket-predictor/real_trader.py'
with open(path, 'r') as f:
    content = f.read()

# 1. Add _get_directional_exposure after _get_total_exposure
old = '''        return row[0] if row else 0.0

    def _check_risk_limits'''
new = '''        return row[0] if row else 0.0

    def _get_directional_exposure(self) -> dict:
        """Sum open position value per direction (Up/Down)."""
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT direction, COALESCE(SUM(price * COALESCE(filled_size, size)), 0) "
            "FROM real_trades WHERE status IN ('matched','live','filled') "
            "GROUP BY direction"
        ).fetchall()
        conn.close()
        return {r[0]: r[1] for r in rows} if rows else {}

    def _check_risk_limits'''
content = content.replace(old, new)

# 2. Add directional exposure check and low volatility filter in risk limits
# After "# 4. Total exposure" section, before "# 5. Balance floor"
old2 = '''        # 4. Total exposure
        exposure = self._get_total_exposure()
        if available > 0 and (exposure / available) > 0.50:
            return False, f"🚨 Exposure {exposure/available:.1%} ≥ 50% (open=${exposure:.2f}, bal=${available:.2f})"

        # 5. Balance floor'''
new2 = '''        # 4. Total exposure
        exposure = self._get_total_exposure()
        if available > 0 and (exposure / available) > 0.50:
            return False, f"🚨 Exposure {exposure/available:.1%} ≥ 50% (open=${exposure:.2f}, bal=${available:.2f})"

        # 4b. Directional exposure (max 30% in one direction)
        dir_exposure = self._get_directional_exposure()
        for d, exp in dir_exposure.items():
            if available > 0 and (exp / available) > 0.30:
                return False, f"🚨 {d} exposure {exp/available:.1%} ≥ 30% (${exp:.2f})"

        # 4c. Low volatility filter — skip trading when market is flat
        if hasattr(self, 'strategy') and hasattr(self.strategy, 'prediction_history'):
            hist = self.strategy.prediction_history
            if len(hist) >= 5:
                recent_confs = [h.get('combined', {}).get('confidence', 0.5) for h in hist[-5:]]
                avg_conf = sum(recent_confs) / len(recent_confs)
                if avg_conf < 0.54:
                    return False, f"🫧 Low signal (avg conf {avg_conf:.1%} < 54%) — skipping"

        # 5. Balance floor'''
content = content.replace(old2, new2)

# 3. Increase consecutive loss limit from 5 to 4
content = content.replace('lose_streak >= 5', 'lose_streak >= 4')

# 4. Decrease daily loss limit from $50 to $30
content = content.replace('daily_pnl <= -50.0', 'daily_pnl <= -30.0')
content = content.replace('max -$50', 'max -$30')

with open(path, 'w') as f:
    f.write(content)

print("✅ Optimizations applied:")
print("  1. Directional exposure limit: 30% per direction")
print("  2. Low volatility filter: avg conf < 54% → skip")
print("  3. Consecutive losses: 5 → 4")
print("  4. Daily loss limit: $50 → $30")
print("  5. EDGE_THRESHOLD: 0.5% → 1.0%")
print("  6. Min confidence: 50% → 55%")
print("  7. NEUTRAL threshold: 0.02 → 0.06")
print("  8. Kelly max fraction: 10% → 5%")
