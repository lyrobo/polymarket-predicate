#!/usr/bin/env python3
"""
Settle open positions by querying Polymarket's official resolution.
Replaces the old Open-Meteo + 48h buffer approach.
"""
import sqlite3, subprocess, json, re, time, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "weather_alpha_v3.db"
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SETTLE] %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "settle_v3.log"), logging.StreamHandler()]
)
logger = logging.getLogger("settle_v3")

HKT = timezone(timedelta(hours=8))
CURL = ["/usr/bin/env", "-u", "SSL_CERT_FILE", "-u", "REQUESTS_CA_BUNDLE",
        "/usr/bin/curl", "-s", "-L", "--max-time", "20"]


def parse_threshold(slug):
    """Extract threshold string from slug like '...36c' or '...24corbelow'"""
    m = re.search(r'-(\d{1,3}(?:-\d{1,3})?[cf](?:orhigher|orbelow)?)$', slug)
    return m.group(1) if m else None


def get_resolution(slug, threshold_str):
    """Query Polymarket for the official resolution of this specific threshold market."""
    url = "https://polymarket.com/market/" + slug
    r = subprocess.run(CURL + [url], capture_output=True, text=True, timeout=25)
    html = r.stdout

    # Find all condition_ids on the event page
    cids = re.findall(r'"conditionId":"(0x[a-fA-F0-9]+)"', html)
    if not cids:
        logger.warning("   ⚠️ No condition_ids found for %s", slug[:60])
        return None

    # For each cid, find the one matching our threshold
    for cid in set(cids):
        url2 = "https://clob.polymarket.com/markets/" + cid
        r2 = subprocess.run(CURL + [url2], capture_output=True, text=True, timeout=18)
        try:
            data = json.loads(r2.stdout)
        except:
            continue

        question = data.get("question", "")
        # Normalize: strip degree symbols, spaces, compare numeric threshold
        q_norm = question.lower().replace('°', '').replace('°', '').replace(' ', '')
        t_norm = threshold_str.lower().replace('°', '').replace('°', '')
        if threshold_str and t_norm not in q_norm:
            continue

        # Found the right market — check resolution
        tokens = data.get("tokens", [])
        winners = [t.get("outcome") for t in tokens if t.get("winner") is True]
        if winners:
            return winners[0]  # "Yes" or "No"
        else:
            return None  # Market found but not yet resolved

    return None  # Market not found (shouldn't happen)


def main():
    if not DB_PATH.exists():
        logger.warning("DB not found: %s", DB_PATH)
        return

    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row

    cur = db.execute(
        "SELECT id, slug, signal, shares, cost FROM positions WHERE status='open'"
    )
    open_positions = cur.fetchall()

    if not open_positions:
        logger.info("No open positions to settle")
        db.close()
        return

    logger.info("Checking %d open positions against Polymarket...", len(open_positions))

    settled = 0
    for pos in open_positions:
        pid = pos["id"]
        slug = pos["slug"]
        signal = pos["signal"]
        shares = pos["shares"]
        cost = pos["cost"]

        threshold = parse_threshold(slug)
        winner = get_resolution(slug, threshold)

        if winner is None:
            continue  # Not yet resolved on PM

        our_side = "No" if signal == "BUY NO" else ("Yes" if signal == "BUY YES" else signal)
        won = our_side == winner
        pnl = round(shares - cost, 4) if won else round(-cost, 4)

        now = datetime.now(HKT).isoformat()
        db.execute(
            "UPDATE positions SET status='resolved', resolved_at=?, resolved_pnl=?, outcome=? WHERE id=?",
            (now, pnl, winner, pid)
        )
        db.commit()

        emoji = "🟢" if won else "🔴"
        logger.info("   %s #%d %s | PM=%s → %s | PnL=$%.2f",
                    emoji, pid, signal, winner, "WIN" if won else "LOSE", pnl)
        settled += 1
        time.sleep(1.5)

    if settled == 0:
        logger.info("No new PM resolutions this run")
    else:
        logger.info("✅ Settled %d positions", settled)

    db.close()


if __name__ == "__main__":
    main()
