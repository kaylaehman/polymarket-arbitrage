#!/usr/bin/env python3
"""Daily maker/longshot validation report -> Discord #trading.
Reads the persisted directional paper DB, computes resolved win-rate vs the
93% backtest, and posts a summary. Runs locally on docker-services (the data
is LAN-only), invoked by root cron.
"""
import sqlite3, os, json, urllib.request, datetime

BASE = "/home/kayla/docker/polymarket-arbitrage"
DB = f"{BASE}/data/directional.db"
BACKTEST_WR = 93  # % win-rate from the 115-trade backtest

def webhook():
    try:
        for ln in open(f"{BASE}/.env"):
            if ln.startswith("ALERT_DISCORD_WEBHOOK="):
                return ln.split("=", 1)[1].strip()
    except Exception:
        pass
    return None

def main():
    c = sqlite3.connect(DB)
    rows = list(c.execute(
        "select market_id, strategy, status, entry_price, realized_pnl "
        "from directional_positions"))
    maker = [r for r in rows if r[1] == "maker_longshot"]
    open_n = [r for r in maker if r[2] == "open"]
    closed = [r for r in maker if r[2] not in ("open", "pending")]
    wins = [r for r in closed if (r[4] or 0) > 0]
    losses = [r for r in closed if (r[4] or 0) < 0]
    flat = [r for r in closed if (r[4] or 0) == 0]  # TTL-cancelled etc.
    resolved = len(wins) + len(losses)
    wr = (len(wins) / resolved * 100) if resolved else None
    realized = sum((r[4] or 0) for r in closed)

    d = datetime.date.today().isoformat()
    L = [f"\U0001F4CA **Maker daily report** — {d}"]
    if resolved:
        delta = wr - BACKTEST_WR
        L.append(f"Resolved: **{resolved}** (W{len(wins)}/L{len(losses)}) | "
                 f"**win-rate {wr:.0f}%** vs backtest {BACKTEST_WR}% "
                 f"({delta:+.0f}pp)")
    else:
        L.append(f"Resolved: **0** yet — nothing has settled; "
                 f"win-rate vs backtest {BACKTEST_WR}% pending first resolutions.")
    L.append(f"Realized P&L: **${realized:+.2f}** | open: {len(open_n)}"
             + (f" | cancelled/flat: {len(flat)}" if flat else ""))
    if losses:
        worst = min(losses, key=lambda r: r[4])
        L.append(f"Worst loss: ${worst[4]:+.2f} on {worst[0]}")
    if open_n:
        L.append("Open: " + ", ".join(
            f"{r[0].split(':')[-1]} NO@{r[3]}" for r in open_n[:6]))
    msg = "\n".join(L)

    wh = webhook()
    if wh:
        data = json.dumps({"content": msg}).encode()
        req = urllib.request.Request(
            wh, data=data, headers={"Content-Type": "application/json", "User-Agent": "polymarket-arb-report/1.0"})
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print("discord post failed:", e)
    print(msg)

if __name__ == "__main__":
    main()
