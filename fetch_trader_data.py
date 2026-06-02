#!/usr/bin/env python3
"""Fetch and analyze trade history for Polymarket trader 0xcE25E214D5cfE4f459cf67F08DF581885AAE7Fdc"""

import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone

TARGET = "0xcE25E214D5cfE4f459cf67F08DF581885AAE7Fdc"
DATA_API = "https://data-api.polymarket.com"

def curl_json(url):
    try:
        r = subprocess.run(
            ["curl", "-s", "--connect-timeout", "5", "--max-time", "15", url],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        return json.loads(r.stdout)
    except:
        return None

def fetch_all_activity(limit_per_page=100, max_pages=10):
    """Fetch all activity pages for the trader."""
    all_activity = []
    for offset in range(0, max_pages * limit_per_page, limit_per_page):
        url = f"{DATA_API}/activity?user={TARGET}&limit={limit_per_page}&offset={offset}"
        data = curl_json(url)
        if not data or not isinstance(data, list) or len(data) == 0:
            break
        all_activity.extend(data)
        print(f"  Page {offset//limit_per_page + 1}: {len(data)} entries (total: {len(all_activity)})")
        if len(data) < limit_per_page:
            break
    return all_activity

def fetch_positions():
    """Fetch current positions."""
    url = f"{DATA_API}/positions?user={TARGET}&sortBy=CURRENT&sortDirection=DESC&sizeThreshold=.1&limit=50"
    return curl_json(url) or []

def fetch_value():
    """Fetch total portfolio value."""
    url = f"{DATA_API}/value?user={TARGET}"
    data = curl_json(url)
    if data and isinstance(data, list) and data:
        return data[0].get("value", 0)
    return 0

def ts_to_dt(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def main():
    print("=" * 70)
    print(f"Polymarket Trader Analysis: {TARGET}")
    print(f"Pseudonym: Agile-Spacing")
    print("=" * 70)
    
    # 1. Portfolio value
    value = fetch_value()
    print(f"\n💰 Current Portfolio Value: ${value:,.2f}")
    
    # 2. Current positions
    print("\n📊 CURRENT POSITIONS:")
    positions = fetch_positions()
    
    total_initial = 0
    total_current = 0
    total_pnl = 0
    
    for pos in positions:
        title = pos.get("title", "?")
        outcome = pos.get("outcome", "?")
        size = pos.get("size", 0)
        avg_price = pos.get("avgPrice", 0)
        cur_price = pos.get("curPrice", 0)
        initial_val = pos.get("initialValue", 0)
        current_val = pos.get("currentValue", 0)
        cash_pnl = pos.get("cashPnl", 0)
        pct_pnl = pos.get("percentPnl", 0)
        
        total_initial += initial_val
        total_current += current_val
        total_pnl += cash_pnl
        
        print(f"  [{outcome:5s}] {title}")
        print(f"         Size={size:.1f}  AvgEntry=${avg_price:.4f}  Cur=${cur_price:.4f}  "
              f"Value=${current_val:.2f}  PnL=${cash_pnl:+.2f} ({pct_pnl:+.1f}%)")
    
    print(f"\n  Total Positions: Initial=${total_initial:.2f}  Current=${total_current:.2f}  PnL=${total_pnl:+.2f}")
    
    # 3. Activity / Trade history
    print("\n🔍 FETCHING ACTIVITY HISTORY...")
    activity = fetch_all_activity(limit_per_page=100, max_pages=10)
    
    if not activity:
        print("  No activity found!")
        return
    
    # Filter to only TRADE type
    trades = [a for a in activity if a.get("type") == "TRADE"]
    print(f"\n📈 TOTAL TRADES: {len(trades)}")
    
    # Group by market (conditionId)
    by_market = defaultdict(list)
    for t in trades:
        by_market[t.get("conditionId", "unknown")].append(t)
    
    # Process each trade group
    print("\n📋 TRADE HISTORY BY MARKET:")
    print("-" * 70)
    
    for cid, market_trades in sorted(by_market.items(), key=lambda x: min(t["timestamp"] for t in x[1])):
        # Get market info from first trade
        first = market_trades[0]
        title = first.get("title", "Unknown")
        slug = first.get("slug", "")
        
        # Sort trades by timestamp
        market_trades.sort(key=lambda x: x["timestamp"])
        
        # Separate by outcome
        by_outcome = defaultdict(list)
        for t in market_trades:
            by_outcome[t.get("outcome", "?")].append(t)
        
        print(f"\n  Market: {title}")
        print(f"  Slug: {slug}")
        print(f"  Condition: {cid[:20]}...")
        
        for outcome, otrades in by_outcome.items():
            total_bought = sum(t["size"] for t in otrades if t.get("side") == "BUY")
            total_sold = sum(t["size"] for t in otrades if t.get("side") == "SELL")
            total_usdc_bought = sum(t.get("usdcSize", 0) for t in otrades if t.get("side") == "BUY")
            total_usdc_sold = sum(t.get("usdcSize", 0) for t in otrades if t.get("side") == "SELL")
            
            # Calculate avg entry
            buys = [t for t in otrades if t.get("side") == "BUY"]
            if buys:
                weighted_price = sum(t["price"] * t["size"] for t in buys) / sum(t["size"] for t in buys)
                print(f"\n    [{outcome}]  {len(otrades)} trades:")
                print(f"      Total Bought: {total_bought:.2f} shares (${total_usdc_bought:.2f})")
                print(f"      Total Sold:   {total_sold:.2f} shares (${total_usdc_sold:.2f})")
                print(f"      Avg Buy Price: ${weighted_price:.4f}")
                print(f"      Trades:")
                for t in otrades:
                    ts = ts_to_dt(t["timestamp"])
                    print(f"        {t['side']:4s} {t['size']:8.2f} @ ${t['price']:.4f} = ${t.get('usdcSize',0):.2f}  [{ts}]")
            else:
                sells = [t for t in otrades if t.get("side") == "SELL"]
                if sells:
                    print(f"\n    [{outcome}]  {len(otrades)} SELL trades:")
                    for t in sells:
                        ts = ts_to_dt(t["timestamp"])
                        print(f"        SELL {t['size']:8.2f} @ ${t['price']:.4f} = ${t.get('usdcSize',0):.2f}  [{ts}]")
    
    # 4. PnL breakdown by market
    print("\n\n💰 PnL BREAKDOWN BY MARKET (from positions):")
    print("-" * 70)
    
    for pos in positions:
        title = pos.get("title", "?")
        outcome = pos.get("outcome", "?")
        cash_pnl = pos.get("cashPnl", 0)
        pct_pnl = pos.get("percentPnl", 0)
        end_date = pos.get("endDate", "?")
        print(f"  {title} [{outcome}]  PnL=${cash_pnl:+.2f} ({pct_pnl:+.1f}%)  Expires: {end_date}")
    
    # 5. Timing patterns
    print("\n\n⏱️ TIMING PATTERNS:")
    print("-" * 70)
    
    # Analyze time between trades
    timestamps = sorted([t["timestamp"] for t in trades])
    if len(timestamps) > 1:
        gaps = [timestamps[i+1] - timestamps[i] for i in range(len(timestamps)-1)]
        avg_gap = sum(gaps) / len(gaps)
        print(f"  Total trades: {len(timestamps)}")
        print(f"  Time range: {ts_to_dt(timestamps[0])} → {ts_to_dt(timestamps[-1])}")
        print(f"  Avg time between trades: {avg_gap:.1f}s")
        print(f"  Min gap: {min(gaps):.1f}s, Max gap: {max(gaps):.1f}s")
    
    # 6. Sizing patterns
    print("\n📏 SIZING PATTERNS:")
    print("-" * 70)
    sizes = [t["size"] for t in trades]
    if sizes:
        print(f"  Min size: {min(sizes):.2f}")
        print(f"  Max size: {max(sizes):.2f}")
        print(f"  Avg size: {sum(sizes)/len(sizes):.2f}")
        print(f"  Median size: {sorted(sizes)[len(sizes)//2]:.2f}")
    
    # 7. Market type breakdown
    print("\n🏷️ MARKET TYPE DISTRIBUTION:")
    slugs = defaultdict(int)
    for t in trades:
        s = t.get("slug", "unknown")
        # Extract base type
        if "btc-updown" in s:
            slugs["BTC 15m"] += 1
        elif "eth-updown" in s:
            slugs["ETH 15m"] += 1
        elif "sol-updown" in s:
            slugs["SOL 15m"] += 1
        elif "xrp-updown" in s:
            slugs["XRP 15m"] += 1
        else:
            slugs[s] += 1
    for k, v in sorted(slugs.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v} trades")
    
    # 8. Side ratio
    sides = defaultdict(int)
    for t in trades:
        sides[t.get("side", "?")] += 1
    print(f"\n📊 BUY/SELL RATIO: {dict(sides)}")
    
    # Save all data
    output = {
        "trader": TARGET,
        "pseudonym": "Agile-Spacing",
        "portfolio_value": value,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "positions": positions,
        "total_trades": len(trades),
        "trades": trades,
    }
    
    outfile = "/home/lyrobo/btc-polymarket-predictor/data/trader_0xce25_analysis.json"
    with open(outfile, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n✅ Full data saved to: {outfile}")
    
    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY:")
    print(f"  Portfolio: ${value:,.2f}")
    print(f"  Open Positions: {len(positions)}")
    print(f"  Total Trades Recorded: {len(trades)}")
    print(f"  Unrealized PnL: ${total_pnl:+.2f}")
    print(f"  Markets Traded: {len(by_market)}")
    print("=" * 70)


if __name__ == "__main__":
    main()
