#!/usr/bin/env python3
"""
Deploy BTC Polymarket Predictor to remote server via SSH.
Usage: python3 deploy_to_server.py
"""

import paramiko
import os
import sys
import time

# Server configuration
SERVER_IP = "8.210.151.164"
SERVER_USER = "root"
SERVER_PASS = "Wjdd7016033@"
PROJECT_DIR = "/opt/btc-polymarket-predictor"
LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))

def run_ssh_command(ssh, cmd, timeout=60):
    """Run a command on the remote server and return output."""
    print(f"  $ {cmd}")
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    exit_status = stdout.channel.recv_exit_status()
    output = stdout.read().decode('utf-8', errors='replace')
    error = stderr.read().decode('utf-8', errors='replace')
    if exit_status != 0 and error.strip():
        print(f"  Error: {error.strip()}")
    return output, exit_status

def upload_file(ssh, local_path, remote_path):
    """Upload a file to the remote server."""
    sftp = ssh.open_sftp()
    sftp.put(local_path, remote_path)
    sftp.close()
    print(f"  ✓ Uploaded: {os.path.basename(local_path)}")

def deploy():
    print("=" * 60)
    print("🔮 BTC 5-Min Polymarket Predictor - Remote Deployment")
    print("=" * 60)
    print(f"   Server: {SERVER_USER}@{SERVER_IP}")
    print(f"   Target: {PROJECT_DIR}")
    print()

    # Connect to server
    print("🔌 Connecting to server...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(SERVER_IP, username=SERVER_USER, password=SERVER_PASS, timeout=10)
        print("  ✓ Connected!")
    except Exception as e:
        print(f"  ✗ Connection failed: {e}")
        print("\n  Troubleshooting:")
        print("  1. Check server IP and password")
        print("  2. Ensure SSH port 22 is accessible")
        print("  3. On server, run: sed -i 's/#PasswordAuthentication yes/PasswordAuthentication yes/' /etc/ssh/sshd_config && systemctl restart sshd")
        sys.exit(1)

    # Step 1: Install dependencies
    print("\n📦 Installing system dependencies...")
    run_ssh_command(ssh, "apt-get update -qq")
    run_ssh_command(ssh, "apt-get install -y -qq python3 python3-pip net-tools curl > /dev/null 2>&1")

    # Step 2: Create directories
    print("\n📁 Creating project directory...")
    run_ssh_command(ssh, f"mkdir -p {PROJECT_DIR}/data {PROJECT_DIR}/logs {PROJECT_DIR}/models {PROJECT_DIR}/venv")

    # Step 3: Create virtual environment
    print("\n🐍 Creating Python virtual environment...")
    run_ssh_command(ssh, f"python3 -m venv {PROJECT_DIR}/venv")

    # Step 4: Install Python packages
    print("\n📦 Installing Python packages...")
    run_ssh_command(ssh, f"{PROJECT_DIR}/venv/bin/pip install --upgrade pip -q")
    run_ssh_command(ssh, f"{PROJECT_DIR}/venv/bin/pip install websocket-client numpy matplotlib -q")

    # Step 5: Upload project files
    print("\n📤 Uploading project files...")
    files_to_upload = []
    for root, dirs, files in os.walk(LOCAL_DIR):
        # Skip __pycache__, venv, and large directories
        dirs[:] = [d for d in dirs if d not in ['__pycache__', 'venv', '.git']]
        for f in files:
            if f.endswith(('.py', '.md', '.txt', '.json')) or f == 'deploy.sh':
                local_path = os.path.join(root, f)
                rel_path = os.path.relpath(local_path, LOCAL_DIR)
                files_to_upload.append((local_path, rel_path))

    for local_path, rel_path in files_to_upload:
        remote_path = os.path.join(PROJECT_DIR, rel_path)
        # Ensure remote directory exists
        remote_dir = os.path.dirname(remote_path)
        run_ssh_command(ssh, f"mkdir -p {remote_dir}")
        upload_file(ssh, local_path, remote_path)

    # Step 6: Upload data directory (if exists)
    data_dir = os.path.join(LOCAL_DIR, "data")
    if os.path.exists(data_dir):
        print("\n📤 Uploading data directory...")
        for f in os.listdir(data_dir):
            if f.endswith('.db'):
                local_path = os.path.join(data_dir, f)
                remote_path = os.path.join(PROJECT_DIR, "data", f)
                upload_file(ssh, local_path, remote_path)

    # Step 7: Create systemd services
    print("\n⚙️  Creating systemd services...")

    trader_service = f"""[Unit]
Description=BTC Polymarket Predictor - Multi-Strategy Trader
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory={PROJECT_DIR}
ExecStart={PROJECT_DIR}/venv/bin/python3 multi_strategy_trader.py --interval 30 --balance 100 --strategies up,down
Restart=always
RestartSec=10
StandardOutput=append:{PROJECT_DIR}/logs/multi_strategy.log
StandardError=append:{PROJECT_DIR}/logs/multi_strategy_error.log

[Install]
WantedBy=multi-user.target
"""

    dashboard_service = f"""[Unit]
Description=BTC Polymarket Predictor - Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory={PROJECT_DIR}
ExecStart=/usr/bin/python3 dashboard_v3.py
Restart=always
RestartSec=10
StandardOutput=append:{PROJECT_DIR}/logs/dashboard.log
StandardError=append:{PROJECT_DIR}/logs/dashboard_error.log

[Install]
WantedBy=multi-user.target
"""

    # Write service files
    run_ssh_command(ssh, f"cat > /etc/systemd/system/btc-predictor-trader.service << 'EOF'\n{trader_service}EOF")
    run_ssh_command(ssh, f"cat > /etc/systemd/system/btc-predictor-dashboard.service << 'EOF'\n{dashboard_service}EOF")
    run_ssh_command(ssh, "systemctl daemon-reload")

    # Step 8: Configure firewall
    print("\n🔥 Configuring firewall...")
    run_ssh_command(ssh, "ufw allow 8765/tcp 2>/dev/null || firewall-cmd --permanent --add-port=8765/tcp 2>/dev/null || echo 'No firewall configured (check cloud security group)'")

    # Step 9: Enable and start services
    print("\n🚀 Starting services...")
    run_ssh_command(ssh, "systemctl enable btc-predictor-trader btc-predictor-dashboard")
    run_ssh_command(ssh, "systemctl restart btc-predictor-trader")
    time.sleep(2)
    run_ssh_command(ssh, "systemctl restart btc-predictor-dashboard")

    # Step 10: Verify
    print("\n🔍 Verifying deployment...")
    time.sleep(3)
    
    # Check trader service
    output, status = run_ssh_command(ssh, "systemctl is-active btc-predictor-trader")
    trader_status = "✅ Running" if status == 0 else "❌ Failed"
    print(f"  Trader: {trader_status}")
    
    # Check dashboard service
    output, status = run_ssh_command(ssh, "systemctl is-active btc-predictor-dashboard")
    dashboard_status = "✅ Running" if status == 0 else "❌ Failed"
    print(f"  Dashboard: {dashboard_status}")
    
    # Check port
    output, _ = run_ssh_command(ssh, "netstat -tlnp 2>/dev/null | grep 8765 || ss -tlnp | grep 8765")
    if "8765" in output:
        print(f"  Port 8765: ✅ Listening")
    else:
        print(f"  Port 8765: ⚠️ Not listening yet (may need more time)")

    # Close SSH
    ssh.close()

    # Summary
    print("\n" + "=" * 60)
    print("✅ Deployment Complete!")
    print("=" * 60)
    print(f"\n📊 Dashboard: http://{SERVER_IP}:8765")
    print(f"\n📋 Management Commands:")
    print(f"  Start:  systemctl start btc-predictor-trader btc-predictor-dashboard")
    print(f"  Stop:   systemctl stop btc-predictor-trader btc-predictor-dashboard")
    print(f"  Status: systemctl status btc-predictor-trader btc-predictor-dashboard")
    print(f"  Logs:   journalctl -u btc-predictor-trader -f")
    print(f"          journalctl -u btc-predictor-dashboard -f")
    print(f"\n⚠️  Important:")
    print(f"  1. Check cloud provider security group - ensure port 8765 is open")
    print(f"  2. Initial WebSocket data takes ~15 seconds to arrive")
    print(f"  3. Monitor logs: ssh root@{SERVER_IP} 'tail -f {PROJECT_DIR}/logs/*.log'")
    print()

if __name__ == "__main__":
    deploy()
