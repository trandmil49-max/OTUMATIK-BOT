# Binance/BingX Futures Analysis & Autonomous Trading Platform V8.0 --
# production image.
#
# Market-data analysis (candles, funding, smart money) reads exclusively
# from Binance's public endpoints. When autonomous trading is switched
# on (RunMode.LIVE + TRADING_ENABLED=true), real order execution goes to
# BingX instead -- see engines/trade_execution.py and
# infrastructure/bingx/client.py's module docstrings for why. With
# either TRADING_ENABLED unset/false, the platform remains signal-only:
# it only reads public market data and sends Telegram messages, exactly
# as before this pivot.

FROM python:3.12-slim

WORKDIR /app

# System-level build deps for any package that needs to compile (none of
# requirements.txt currently does, but this keeps future additions safe
# without needing to revisit the base image).
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# data/ (SQLite database + backups) and logs/ (rotating log files) are
# runtime-created and .gitignore'd -- ensure they exist as writable
# directories in the image rather than relying on first-run creation
# inside a read-only-by-default container filesystem layer.
RUN mkdir -p data logs data/backups

# Railway (and most PaaS runners) inject PORT for HTTP services; this
# platform has no HTTP server -- it is a long-running background worker
# communicating outward via the Binance/Telegram REST APIs only. No
# EXPOSE needed.

# Unbuffered stdout so log lines reach Railway's log viewer immediately
# rather than sitting in Python's output buffer.
ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
