#!/bin/bash
# BTC Polymarket Predictor - Deployment Script
# Run this script as root on the target server:
#   bash deploy.sh

set -e

echo "=========================================="
echo "🔮 BTC 5-Min Polymarket Predictor Setup"
echo "=========================================="

# 1. Install dependencies
echo ""
echo "📦 Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv net-tools curl > /dev/null 2>&1

# 2. Create project directory
PROJECT_DIR="/opt/btc-polymarket-predictor"
echo ""
echo "📁 Creating project directory: $PROJECT_DIR"
mkdir -p "$PROJECT_DIR"
mkdir -p "$PROJECT_DIR/data"
mkdir -p "$PROJECT_DIR/logs"
mkdir -p "$PROJECT_DIR/models"

# 3. Create Python virtual environment
echo ""
echo "🐍 Setting up Python environment..."
python3 -m venv "$PROJECT_DIR/venv"
source "$PROJECT_DIR/venv/bin/activate"

# 4. Install Python dependencies
echo ""
echo "📦 Installing Python packages..."
pip install --upgrade pip -q
pip install websocket-client numpy matplotlib -q

# Check if py-clob-client-v2 is needed (for real trading)
# pip install py-clob-client-v2 -q  # Uncomment if doing real trading

echo ""
echo "✅ Python environment ready"
echo "   Python: $(which python3)"
echo "   pip packages: websocket-client, numpy, matplotlib"

# 5. Create systemd service files
echo ""
echo "⚙️  Creating systemd services..."

# Multi-strategy trader service
cat > /etc/systemd/system/btc-predictor-trader.service << 'EOF'
[Unit]
Description=BTC Polymarket Predictor - Multi-Strategy Trader
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/btc-polymarket-predictor
ExecStart=/opt/btc-polymarket-predictor/venv/bin/python3 multi_strategy_trader.py --interval 30 --balance 100 --strategies up,down
Restart=always
RestartSec=10
StandardOutput=append:/opt/btc-polymarket-predictor/logs/multi_strategy.log
StandardError=append:/opt/btc-polymarket-predictor/logs/multi_strategy_error.log

[Install]
WantedBy=multi-user.target
EOF

# Dashboard service
cat > /etc/systemd/system/btc-predictor-dashboard.service << 'EOF'
[Unit]
Description=BTC Polymarket Predictor - Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/btc-polymarket-predictor
ExecStart=/usr/bin/python3 dashboard_v3.py
Restart=always
RestartSec=10
StandardOutput=append:/opt/btc-polymarket-predictor/logs/dashboard.log
StandardError=append:/opt/btc-polymarket-predictor/logs/dashboard_error.log

[Install]
WantedBy=multi-user.target
EOF

# Reload systemd
systemctl daemon-reload

echo ""
echo "✅ Systemd services created"
echo "   btc-predictor-trader   - Multi-strategy trading"
echo "   btc-predictor-dashboard - Web dashboard (port 8765)"

# 6. Configure firewall
echo ""
echo "🔥 Configuring firewall..."
if command -v ufw &> /dev/null; then
    ufw allow 8765/tcp comment "BTC Predictor Dashboard" || true
    echo "   UFW: Port 8765 opened"
elif command -v firewall-cmd &> /dev/null; then
    firewall-cmd --permanent --add-port=8765/tcp || true
    firewall-cmd --reload || true
    echo "   firewalld: Port 8765 opened"
else
    echo "   No firewall detected (check cloud provider security group)"
fi

echo ""
echo "=========================================="
echo "📋 Next Steps:"
echo "=========================================="
echo ""
echo "1. Copy project files to the server:"
echo "   From your local machine, run:"
echo ""
echo "   cd /home/lyrobo/btc-polymarket-predictor"
echo "   scp -r *.py data/ models/ root@8.210.151.164:/opt/btc-polymarket-predictor/"
echo ""
echo "2. Enable and start services:"
echo "   systemctl enable --now btc-predictor-trader"
echo "   systemctl enable --now btc-predictor-dashboard"
echo ""
echo "3. Access dashboard:"
echo "   http://8.210.151.164:8765"
echo ""
echo "4. Check logs:"
echo "   journalctl -u btc-predictor-trader -f"
echo "   journalctl -u btc-predictor-dashboard -f"
echo ""
echo "5. Stop trading (if needed):"
echo "   systemctl stop btc-predictor-trader"
echo ""
echo "=========================================="
echo "✅ Setup script complete!"
echo "=========================================="
