# SETUP.md — Getting Started

Step-by-step from zero to running bot with intelligence layer.

---

## 1. Fork and Clone

```bash
# Fork ImMike/polymarket-arbitrage on GitHub first, then:
git clone https://github.com/YOUR_USERNAME/polymarket-arbitrage.git
cd polymarket-arbitrage

# Add upstream for future syncs
git remote add upstream https://github.com/ImMike/polymarket-arbitrage.git
```

---

## 2. Python Environment

```bash
python -m venv venv
source venv/bin/activate        # Linux/Mac
# venv\Scripts\activate         # Windows

pip install -r requirements.txt
```

Python 3.10+ required.

---

## 3. API Keys

### NewsAPI (free — 500 req/day)
1. Sign up at https://newsapi.org
2. Copy your API key

### Anthropic API
- Use your existing key from console.anthropic.com
- OR route through OpenClaw at localhost:3456 (set `ANTHROPIC_BASE_URL`)

### Polymarket + Kalshi (only needed for live trading — skip for now)
- Polymarket: create wallet, export private key
- Kalshi: create account at kalshi.com, generate API key

### Set environment variables

```bash
# Create .env file (already in .gitignore)
cat > .env << 'EOF'
NEWSAPI_KEY=your_newsapi_key_here
ANTHROPIC_API_KEY=your_anthropic_key_here
ANTHROPIC_BASE_URL=http://localhost:3456   # delete this line if not using OpenClaw

# Leave these blank until ready for live trading
POLYMARKET_API_KEY=
POLYMARKET_PRIVATE_KEY=
KALSHI_API_KEY=
EOF
```

Load them:
```bash
export $(grep -v '^#' .env | xargs)
```

Or use `python-dotenv` — add `from dotenv import load_dotenv; load_dotenv()` to `main.py`.

---

## 4. Config

Edit `config.yaml`:

```yaml
mode:
  trading_mode: "dry_run"     # KEEP THIS until you're confident
  data_mode: "simulation"     # Start here. Switch to "real" when ready.
  cross_platform_enabled: true
  kalshi_enabled: true

intelligence:
  enabled: false              # Set true once API keys are set
  mode: "filter"
```

---

## 5. Run in Simulation Mode (no API keys needed)

```bash
python run_with_dashboard.py
```

Open http://localhost:8000 — you should see simulated arb opportunities.

---

## 6. Enable Intelligence Layer

Once you have `NEWSAPI_KEY` and `ANTHROPIC_API_KEY` set:

```yaml
# config.yaml
intelligence:
  enabled: true
  mode: "filter"
```

```bash
python run_with_dashboard.py
```

Watch the logs for `[Intelligence]` lines. You'll see:
- `[Intelligence] Engine initialized`
- `[Intelligence] Fetched 3 articles for "Federal Reserve rate decision"`
- `[Intelligence] Signal for market_xyz: bullish (confidence: 0.71)`
- `[Intelligence] Filtered market_xyz: AI bearish, skipping YES arb`

---

## 7. Switch to Real Data (still dry run)

```yaml
mode:
  trading_mode: "dry_run"
  data_mode: "real"           # Now reads live Polymarket markets
```

Real markets are efficient. You may scan thousands of markets and find zero arb.
That's expected. The intelligence layer becomes more valuable here — directional
signals from news can flag markets worth watching.

---

## 8. Homelab Deployment (optional)

Run as a Docker container on your `docker-services` VM:

```dockerfile
# Dockerfile (create this)
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["python", "run_with_dashboard.py"]
```

```yaml
# docker-compose.yml (create this)
version: "3.8"
services:
  polymarket-bot:
    build: .
    ports:
      - "8123:8000"           # pick a free port on your homelab
    environment:
      - NEWSAPI_KEY=${NEWSAPI_KEY}
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - ANTHROPIC_BASE_URL=http://192.168.20.9:3456   # OpenClaw on docker-services
    volumes:
      - ./config.yaml:/app/config.yaml
      - ./logs:/app/logs
    restart: unless-stopped
```

```bash
docker-compose up -d
```

Dashboard at http://your-homelab-ip:8123

Add to your Homepage dashboard at home.kaylas.systems.

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: intelligence` | Module not scaffolded yet | Run Phase 1 scaffolding first |
| `[Intelligence] Signal failed: timeout` | Claude API slow | Increase `timeout_seconds` in config |
| `[Intelligence] Signal failed: 401` | Bad Anthropic key | Check `ANTHROPIC_API_KEY` env var |
| News always empty | Bad NewsAPI key or rate limit | Check `NEWSAPI_KEY`, check 429 in logs |
| No arb in real mode | Markets are efficient | Normal — intelligence boost mode helps here |
| Dashboard 404 | Port conflict | Change port in `run_with_dashboard.py` |
