"""
Hourly Simulated Trading Report Generator
==========================================
Generates a markdown table of sim trades from the last hour and sends to WeChat.
Usage: python3 hourly_report.py
"""

import sqlite3
import os
from datetime import datetime, timezone, timedelta

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
DB_PATH = os.path.join(DATA_DIR, 'btc_predictor.db')


def generate_report():
    """Generate hourly sim trading report."""
    conn = sqlite3.connect(DB_PATH)
    
    # Get trades from last hour
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    
    trades = conn.execute('''
        SELECT id, timestamp, direction, market_question, amount, fee,
               pnl, win, status, wallet_balance_after, btc_entry_price,
               resolution_price, our_confidence, edge
        FROM sim_trades 
        WHERE timestamp > ? AND strategy = 'UP-ONLY'
        ORDER BY id ASC
    ''', (one_hour_ago,)).fetchall()
    
    # Get portfolio summary
    portfolio = conn.execute('''
        SELECT balance, total_value, total_pnl, total_return_pct, 
               win_rate, wins, losses, total_trades
        FROM sim_portfolio 
        WHERE strategy = 'UP-ONLY'
        ORDER BY id DESC LIMIT 1
    ''').fetchone()
    
    conn.close()
    
    if not trades:
        return f"""📊 BTC 模拟交易报告 | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:00 UTC')}

⏸ 过去1小时内无新押注

💼 账户概况:
   余额: ${portfolio[0]:.2f} (如存在)
   总值: ${portfolio[1]:.2f}
   总盈亏: {'+' if portfolio[2] >= 0 else ''}${portfolio[2]:.2f}
   收益率: {portfolio[3]:.2f}%
   胜率: {portfolio[4]:.1f}% ({portfolio[5]}W/{portfolio[6]}L)
   总交易: {portfolio[7]}
"""
    
    # Build markdown table
    lines = []
    lines.append(f"📊 BTC 模拟交易报告 | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:00 UTC')}")
    lines.append("")
    lines.append("| # | 押注时间 | 押注内容 | 金额 | 结果 | 收益 | 余额 |")
    lines.append("|---|---------|---------|------|------|------|------|")
    
    total_bet = 0
    total_pnl = 0
    wins = 0
    
    for i, t in enumerate(trades, 1):
        trade_id, timestamp, direction, market_q, amount, fee, pnl, win, status, balance, btc_entry, btc_res, conf, edge = t
        
        # Format time
        try:
            dt = datetime.fromisoformat(timestamp)
            time_str = dt.strftime('%m-%d %H:%M')
        except:
            time_str = timestamp[:16]
        
        # Format bet content
        content = f"{direction} @${amount:.2f}"
        if btc_entry > 0:
            content += f" (BTC${btc_entry:,.0f})"
        
        # Format result
        if status == 'resolved':
            result = "✅赢" if win else "❌输"
            if win:
                wins += 1
        else:
            result = "⏸待决"
        
        # Format PnL
        pnl_str = f"+${pnl:.2f}" if pnl > 0 else f"-${abs(pnl):.2f}"
        
        # Format balance
        balance_str = f"${balance:.2f}"
        
        lines.append(f"| {i} | {time_str} | {content} | ${amount:.2f} | {result} | {pnl_str} | {balance_str} |")
        
        total_bet += amount
        total_pnl += pnl
    
    lines.append("")
    lines.append(f"📈 过去1小时统计:")
    lines.append(f"   押注次数: {len(trades)}")
    lines.append(f"   总押注额: ${total_bet:.2f}")
    lines.append(f"   总盈亏: {'+' if total_pnl >= 0 else ''}${total_pnl:.2f}")
    if wins > 0:
        lines.append(f"   胜率: {wins/len(trades)*100:.1f}% ({wins}W/{len(trades)-wins}L)")
    
    if portfolio:
        lines.append("")
        lines.append(f"💼 账户概况:")
        lines.append(f"   余额: ${portfolio[0]:.2f}")
        lines.append(f"   总值: ${portfolio[1]:.2f}")
        lines.append(f"   总盈亏: {'+' if portfolio[2] >= 0 else ''}${portfolio[2]:.2f}")
        lines.append(f"   收益率: {portfolio[3]:.2f}%")
        lines.append(f"   胜率: {portfolio[4]:.1f}% ({portfolio[5]}W/{portfolio[6]}L)")
        lines.append(f"   总交易: {portfolio[7]}")
    
    return "\n".join(lines)


if __name__ == '__main__':
    report = generate_report()
    print(report)
    
    # Save to file for cron job to read
    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'hourly_report.txt')
    with open(output_path, 'w') as f:
        f.write(report)
    print(f"\nReport saved to: {output_path}")
