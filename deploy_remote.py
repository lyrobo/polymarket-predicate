#!/usr/bin/env python3
"""Deploy BTC Polymarket Predictor to remote server."""

import paramiko
import os
import sys
import time
from pathlib import Path

# Config
HOST = "8.210.151.164"
USER = "root"
PASSWORD = "Wjdd7016033@"
REMOTE_DIR = "/opt/btc-polymarket-predictor"
LOCAL_DIR = "/home/lyrobo/btc-polymarket-predictor"

# Files to upload (relative to LOCAL_DIR)
FILES = [
    "multi_strategy_trader.py",
    "dashboard.py",
    "unified_strategy.py",
    "order_flow.py",
    "mean_reversion.py",
    "volatility_breakout.py",
    "event_driven.py",
    "technical_analysis.py",
    "prediction_engine.py",
    "strategy_engine.py",
    "data_collector.py",
    "websocket_collector.py",
    "realtime_service.py",
    "polymarket_client.py",
    "config.py",
    "real_trader.py",
    "sim_trader.py",
    "backtest.py",
    ".env",
    "requirements.txt",
    "POLYMARKET_SETUP.md",
]

# Data/model dirs to create
DIRS = ["data", "logs", "models"]


def ssh_connect():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=PASSWORD, timeout=15)
    return client


def run_cmd(client, cmd, timeout=60):
    """Run command and return stdout, stderr, exit_code."""
    print(f"  $ {cmd[:80]}...")
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode()
    err = stderr.read().decode()
    exit_code = stdout.channel.recv_exit_status()
    return out, err, exit_code


def upload_file(client, local_path, remote_path):
    """Upload a single file via SFTP."""
    sftp = client.open_sftp()
    sftp.put(local_path, remote_path)
    sftp.close()
    print(f"  ✅ {os.path.basename(local_path)}")


def main():
    print(f"🚀 Deploying to {HOST}...")
    
    # 1. Connect
    print("\n📡 Connecting...")
    client = ssh_connect()
    print("  ✅ Connected")
    
    # 2. Install system deps
    print("\n📦 Installing system packages...")
    out, err, rc = run_cmd(client, "apt-get update -qq && apt-get install -y -qq python3 python3-pip python3-venv net-tools 2>&1 | tail -5", timeout=120)
    print(f"  Exit: {rc}")
    
    # 3. Create directories
    print("\n📁 Creating project structure...")
    run_cmd(client, f"mkdir -p {REMOTE_DIR} {' '.join(f'{REMOTE_DIR}/{d}' for d in DIRS)}")
    
    # 4. Upload files
    print("\n📤 Uploading files...")
    for fname in FILES:
        local = os.path.join(LOCAL_DIR, fname)
        remote = os.path.join(REMOTE_DIR, fname)
        if os.path.exists(local):
            upload_file(client, local, remote)
        else:
            print(f"  ⚠️ Skipped (not found): {fname}")
    
    # 5. Install Python deps
    print("\n🐍 Installing Python packages...")
    cmd = (
        f"cd {REMOTE_DIR} && "
        f"python3 -m pip install --break-system-packages "
        f"websocket-client numpy matplotlib scikit-learn 2>&1 | tail -5"
    )
    out, err, rc = run_cmd(client, cmd, timeout=180)
    print(out)
    if err.strip():
        print(f"  stderr: {err[:200]}")
    
    # 6. Kill old processes
    print("\n🧹 Cleaning up old processes...")
    run_cmd(client, "pkill -f multi_strategy_trader 2>/dev/null || true")
    run_cmd(client, "pkill -f dashboard.py 2>/dev/null || true")
    time.sleep(2)
    
    # 7. Start dashboard
    print("\n📊 Starting dashboard...")
    run_cmd(client, f"cd {REMOTE_DIR} && nohup python3 dashboard.py --port 8765 > logs/dashboard.out 2>&1 &")
    time.sleep(2)
    
    # 8. Start trader
    print("\n🏃 Starting trader...")
    run_cmd(client, f"cd {REMOTE_DIR} && nohup python3 multi_strategy_trader.py --interval 30 --balance 100 > logs/trader.out 2>&1 &")
    time.sleep(3)
    
    # 9. Setup firewall
    print("\n🔥 Configuring firewall...")
    run_cmd(client, "ufw allow 8765/tcp 2>/dev/null || iptables -A INPUT -p tcp --dport 8765 -j ACCEPT 2>/dev/null || echo 'FW: manual config needed'")
    
    # 10. Verify
    print("\n✅ Verifying...")
    out, err, rc = run_cmd(client, f"ps aux | grep -E 'multi_strat|dashboard' | grep -v grep")
    print(out)
    
    out, err, rc = run_cmd(client, f"ls -la {REMOTE_DIR}/data/btc_predictor.db 2>/dev/null && echo 'DB OK' || echo 'DB not yet created'")
    print(out)
    
    # 11. Test dashboard
    print("\n🌐 Testing dashboard...")
    out, err, rc = run_cmd(client, "curl -s -o /dev/null -w '%{http_code}' http://localhost:8765/ 2>/dev/null || echo 'not ready'")
    print(f"  HTTP status: {out.strip()}")
    
    print(f"\n{'='*60}")
    print(f"✅ Deployment complete!")
    print(f"   Dashboard: http://{HOST}:8765")
    print(f"   Trader log: ssh root@{HOST} 'tail -f {REMOTE_DIR}/logs/realtime.log'")
    print(f"{'='*60}")
    
    client.close()


if __name__ == "__main__":
    main()
