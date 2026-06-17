FROM python:3.12-slim

WORKDIR /app

# The bot is pure-Python async (httpx/websockets/fastapi); no system build deps needed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8888

# Dry-run by default (safe — no real orders, no wallet key needed).
# To go live, see GOING-LIVE.md and change the compose command to add --live.
CMD ["python", "run_with_dashboard.py", "--config", "config.yaml", "--port", "8888"]
