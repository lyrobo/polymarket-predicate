#!/bin/bash
cd /home/lyrobo/btc-polymarket-predictor
export $(grep -v '^#' .env.131 | xargs)
exec python3 -u real_dashboard.py 2>&1
