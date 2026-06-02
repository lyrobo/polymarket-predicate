#!/bin/bash
# Start Annica sim bot on 164
cd /opt/btc-polymarket-predictor

# Kill existing instances
pkill -f 'annica_sim.py' 2>/dev/null
sleep 1

# Start fresh
nohup python3 annica_sim.py --balance 700 --ratio 0.05 --max-price 0.30 \
  >> logs/annica_sim.log 2>&1 &

echo "Annica sim started PID=$!"
