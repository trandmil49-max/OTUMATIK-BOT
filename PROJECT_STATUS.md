# PROJECT_STATUS.md

_Last verified against the actual filesystem and a full test run on 2026-08-27 (565/565 tests passing, verified from a freshly-extracted ZIP). This document is the authoritative handoff record — if anything here conflicts with assumptions made in a prior conversation, trust this document and the actual files, not memory of past chat._

_Historical note: an earlier version of this file went stale for an extended period — its "REMAINING MODULES"/"NEXT TASK" sections kept describing only Modules 1-2 as complete long after all 22 modules had actually been built, which actively misdirected at least one later session. That stale content has been removed rather than patched further. **Keep this document in sync going forward: a stale status here is worse than no status at all.**_

---

# PROJECT OVERVIEW

**Objective:** "Binance Futures Analysis Platform V8.0" (COLDE-BOT) — a professional/institutional-grade, **signal-only** (no order execution) Binance USDT-M Futures analysis engine, built from a 10,388-line, 23-part Software Requirements Specification (SRS). The platform scans the futures market, applies multi-stage filtering and scoring (trend, structure, momentum, risk, Bitcoin health, market health, coin trust), and delivers high-confidence trade signals via Telegram, with self-learning analytics and Turkish-language self-reporting. No real trades are ever placed by this system.

**Current development stage: feature-complete for SRS scope.** All 22 planned modules are built, wired, and tested. A handful of items are explicitly, deliberately deferred (see KNOWN DEFERRED GAPS) — each documented at its own definition site, none block running end-to-end in LIVE or PAPER mode.

**Overall architecture:** Layered / hexagonal (ports-and-adapters):

```
core/            <- pure domain logic & dataclasses, ZERO I/O dependency (fully unit-testable)
infrastructure/  <- adapters: Binance client, Telegram, SQLite database
application/     <- use-case orchestration (composition_root.py, main.py) — wires core + infrastructure together
execution_modes/ <- live / paper / backtest runners, all reusing the SAME core engines
system/          <- cross-cutting: logging, error handling, retry (no knowledge of infrastructure/)
config/          <- centralized, validated, profile-aware settings (all modules depend on this)
tests/           <- unit tests, mirroring the source tree (565 tests)
```

The non-negotiable rule behind this shape: **`core/` and `system/` must never import from `infrastructure/`.** This is what lets Live, Paper, and Backtest share identical Confidence/Risk/Structure engines (SRS Part 13/20), and what let `system/error_handler.py` be built and fully tested before Telegram existed at all — infrastructure reaches up via `register_alert_callback()`, `core`/`system` never reach down.

---

# MODULE STATUS — all 22 complete

| # | Module | Key file(s) |
|---|---|---|
| 1 | Configuration Engine | `config/schema.py`, `config/loader.py`, `config/defaults.yaml`, `config/profiles/*.yaml` |
| 2 | Logging & Error-Handling Framework | `system/exceptions.py`, `system/logging_setup.py`, `system/error_handler.py`, `system/retry.py` |
| 3 | Database Schema & Repository Layer | `infrastructure/database/schema.py` (2 migrations), `connection.py`, `repositories/*.py`, `core/models.py` |
| 4 | Binance API Client | `infrastructure/binance/client.py` — weight-aware rate limiting, TTL cache, retry on transient errors only |
| 5 | Symbol Discovery & Trading Universe | `engines/symbol_discovery.py` |
| 6 | Technical Indicator Library | `engines/indicators.py` — SMA, EMA, RSI, MACD, ATR, Bollinger Bands, ADX(+DI/-DI), volume_ratio, swing points |
| 7 | Stage 1 — Fast Filter Engine | `engines/fast_filter.py` |
| 8 | Bitcoin Intelligence Engine | `engines/bitcoin_intelligence.py` |
| 9 | Global Market Health Engine | `engines/market_health.py` |
| 10 | Market Structure Engine | `engines/market_structure.py` — BOS/CHoCH/swings/liquidity sweep/order blocks/FVG |
| 11 | Risk Management Engine | `engines/risk_management.py` — dynamic ATR+structure SL/TP/RR, confidence-tiered leverage (max 10x) |
| 12 | Confidence & Decision Engine | `engines/confidence.py` — 6-category weighted blend (trend/structure/risk/bitcoin/coin_trust/market_health), momentum-confirmation adjustment |
| 13 | Coin Trust / Coin Intelligence Engine | `engines/coin_trust.py` |
| 14 | Rejection & Missed-Opportunity Engine | `engines/rejection.py` |
| 15 | Telegram Notification Engine | `engines/telegram_notifications.py` — 14 notification types, `infrastructure/telegram/client.py` (raw aiohttp); `engines/telegram_commands.py` — incoming `/scan /status /rapor /help` commands (2026-08-02) |
| 16 | Trade / Position Monitor | `engines/position_monitor.py` — WAITING→ACTIVE, TP1/TP2/SL/BE/expiry lifecycle |
| 17 | Scanner Orchestrator | `engines/scanner_orchestrator.py` — wires Stages 1-4 into the full async pipeline |
| 18 | Reporting Engine | `engines/reporting.py` — daily/weekly/monthly + Turkish self-analysis |
| 19 | Analytics & Recommendation Engine | `engines/analytics.py` |
| 20 | Production Engine | `engines/production.py` — watchdog, health checks, backups |
| 21 | Paper Trading / Backtest Runner | `execution_modes/backtest.py` |
| 22 | Main Orchestrator & Deployment Wiring | `application/main.py`, `application/composition_root.py`, `main.py`, Railway env vars |

**Full regression: 565/565 passing** (`pytest tests/unit/ -q`), verified from a freshly-extracted ZIP.

---

# KNOWN, DELIBERATELY-DEFERRED GAPS
_(each documented at its own definition site — not fabricated, not silently missing)_

- **FilterPerformance scoring** (`engines/analytics.py`) — the `filter_performance` table exists; no engine populates it. SRS doesn't define the scoring formula.
- **False-positive/negative classification threshold** (`engines/analytics.py`) — same reason.
- **CPU/RAM metrics + a few `BotHealthSnapshot` counters** (`engines/production.py`) — `average_scan_duration_seconds`, `average_api_response_ms`, `symbols_scanned_count`, `retry_count`, `restart_count` stay at dataclass defaults; nothing instruments them as running counters yet.
- **Full-pipeline historical backtesting** (`execution_modes/backtest.py`) — blocked by `book_ticker` having no historical equivalent via Binance's REST API.
- **CSV/Excel/PDF report export** (`engines/reporting.py`) — needs new dependencies, not added.
- **`RiskConfig.max_same_sector_exposure`** (`engines/risk_management.py`) — config field exists, unenforced: no sector-classification data source exists anywhere in this codebase.
- **Extra indicators reviewed but not added** (2026-07-28 audit): VWAP, Donchian Channels, OBV, CMF, MFI, CCI, Supertrend, CVD, Fear & Greed Index. (Long/short account ratio, also on this list at the time of the audit, is no longer deferred -- built as the Smart Money engine, Module 23, using Binance's official top-trader long/short ratio endpoints.) None of the remaining ones are referenced anywhere in `config/schema.py`'s 14 categories or the original module breakdown — adding them would mean inventing scoring logic the SRS never specified, not fixing a gap. Available as a genuine future enhancement if wanted, with its own SRS-style spec for how each should weigh into scoring.

None of these block LIVE or PAPER mode.

---

# SESSION LOG
_(most recent first — brief, factual, dated)_

**2026-08-27 — Scan-cycle crash on Binance 418.** Production crash-looped every ~60-90s for 30+ minutes straight: `engines/scanner_orchestrator.py`'s call to `fast_filter.run()` was the one step in the whole scan cycle with no `_safe_call` isolation (symbol discovery/Bitcoin Intelligence/Market Health all already degrade gracefully on failure; this one didn't). When Binance returned HTTP 418 (rate-limit ban) for longer than the client's retry policy waits, the exception propagated all the way to `execution_modes/live.py`'s outermost handler, logged as CRITICAL "Unhandled exception in scan cycle" -- confirmed from real deploy logs, not assumed. Wrapped in `_safe_call` exactly like the other three phases: a failed Stage 1 now yields zero survivors for that cycle instead of crashing it. Note: this stops the crash-loop; if Binance's IP ban is still active, scanning stays degraded until it naturally expires -- that part isn't something this codebase can control.

**2026-08-08 — Rate-limit starvation + structural risk-scoring fix.** `/scan /status /rapor /help` intermittently failed ("Command poll failed" warnings in production) because `get_updates()` was spending `config.telegram.max_messages_per_minute` (default 20) at weight=1 -- polling every ~3 seconds alone could consume the ENTIRE budget meant for outbound notifications, starving signals/reports/replies of room to send. Fixed to weight=0 (matching `getMe`'s existing precedent -- neither call sends a message to the chat, so neither should compete for that budget), same fix applied to `delete_webhook()`.

Also found and fixed the actual root cause of chronically low confidence scores: `RiskManagementEngine.calculate_take_profits()` defines TP1 as EXACTLY `min_risk_reward` (2.0) multiples of the stop distance by formula, and `_clamp_to_structural_target()` can only pull TP1 CLOSER when a real level is in the way, never extend it further even when the next real structural level is much farther out. The result: the RR fed into `calculate_risk_score()` was, by construction, almost always exactly `min_risk_reward` for every approved signal -- confirmed live, dozens of different symbols in the same scan cycle producing the identical risk=9.3/20. A setup with acres of real structural headroom scored identically to one that barely cleared the minimum. Fixed by scoring against the actual UNCLAMPED distance to the next structural level when one exists, while leaving the traded TP1/TP2/stop-loss and the accept/reject gate completely unchanged (same trades, better-informed confidence about them). Verified against real production numbers from this session: raises a representative setup's total confidence by roughly 5-11 points depending on how much real structural room exists, without touching trend/structure/bitcoin/coin_trust/market_health at all.

**2026-08-02 — Live diagnosis + interactive commands.** Production had generated zero signals for ~2 weeks. Added a per-rejection confidence-breakdown log line (`engines/signal_generation.py`) to actually see why, rather than guess; the real deployed logs then showed `trend`/`structure` scoring perfectly (20/20, 15/15) while `risk`/`bitcoin`/`coin_trust`/`market_health` were chronically weak (e.g. 9.3/20, 5.3/15, 5.3/15, 8.7/15) — even a near-perfect setup capped around 63-64, nowhere near `minimum_confidence`'s default 80. Root cause: the 2026-07-25 session's convex (exponent=1.5) rescaling of those four categories, compounding with `coin_trust` being correctly stuck at a neutral cold-start default (zero closed trades exist yet to build a track record from) — not a bug, but a calibration mismatch between that curve and the threshold, which nobody had checked against real data until now. Recommended lowering `MIN_CONFIDENCE` (a Railway env var, no redeploy needed) as the immediate, reversible lever, rather than touching the deliberately-chosen exponent again. Also fixed a real but minor edge case found while investigating (NaN `macd_histogram` misread as "opposes" instead of "neutral" — `!=` vs. NaN is always True in Python).

Also built `engines/telegram_commands.py`: `/scan /status /rapor /help` now actually work (nothing in the codebase listened for incoming Telegram messages before this — the "Binance Futures signal bot aktif. Komutlar: ..." message the project owner had seen was Telegram's own BotFather command-menu UI, not this platform; confirmed with the project owner that it had never actually responded to any of the four before building this). Runs concurrently with the scan loop (`asyncio.gather` in `application/main.py`) via short-polling (not Telegram long-polling — see that module's docstring for why), filters to the configured `chat_id` only.

**2026-07-28 — Full production audit.** Read the entire codebase end-to-end against the SRS's intent, the Telegram formatting spec, and general professional-trading-system practice. Found and fixed 8 real issues (all with new/extended tests, 506 → 529 tests):
1. RSI/MACD/ADX/volume_ratio were computed but never consumed by any engine — now feed a bounded ±15% momentum-confirmation/divergence multiplier into `ConfidenceEngine`'s trend score (see `engines/confidence.py`'s module docstring).
2. TP1 partial-exit price/profit % was never recorded (`Trade` had no field for it) — added `tp1_exit_price` (migration v2), `Trade.tp1_pnl_percent`, and it now appears in the TP1 Telegram message.
3. A WAITING signal that expired without triggering produced zero notification — added `notify_signal_expired()` (the 14th notification type the module docstring already claimed existed) and wired it into `execution_modes/live.py`'s dispatch.
4. `BINANCE_TIMEOUT_SECONDS` was the 6th Railway tuning env var silently ignored (5 of 6 were fixed in the prior session; this one was missed).
5. WARNING/ERROR severity errors never actually reached Telegram — `notify_warning()` existed and was wired to route them, but `system/error_handler.py`'s fan-out gate only ever triggered for CRITICAL/FATAL, so that routing code was unreachable. Gate now includes WARNING/ERROR.
6. `ErrorEventRepository` was never written to by anything — `ProductionEngine`'s "unresolved_critical" health counter was permanently 0. Added a second alert callback (`application/composition_root.py`) that persists CRITICAL/FATAL errors.
7. Leverage tiers were hardcoded to the *default* confidence grade bands (95/90/85/80) instead of reading the active strategy profile's own bands — under the `professional` profile specifically, this handed out full 10x leverage to signals not yet graded INSTITUTIONAL under that profile's own (stricter) standard. Now reads `ConfidenceConfig` at call time.
8. `bitcoin_intelligence.py`'s docstring claimed a `RejectionReason.BITCOIN_CONFLICT` rejection was surfaced downstream; no engine anywhere ever assigns that enum member. Docstring corrected to describe the actual mechanism (bitcoin_score → blended confidence → generic LOW_CONFIDENCE).

Also regenerated this file (was self-contradictory — see historical note above) and `README.md`'s stale test count.

**Open question surfaced, not resolved unilaterally:** the Telegram signal/TP1/TP2/SL/trade-closed message templates are in Turkish; a reference spec provided in the same session illustrated the same emoji system in English. Confirmed with the project owner to keep Turkish — no code change made on this point.

**2026-07-25/26 — Telegram redesign + leverage + confidence curve + env var fixes** (prior session, `V8_UPDATE_FINAL` delta package). Full Telegram notification redesign (14 types, consistent skeleton, Europe/Istanbul timestamps); fixed a duplicate notification bug (TP2/SL were sending both a "hit" ping and `notify_trade_closed`); introduced the confidence-tiered leverage table (95/90/85/80/75 → 10/8/7/5/3x, max 10x); made Risk/Bitcoin/Coin Trust/Market Health scoring exponential (1.5) instead of linear, so mediocre inputs earn less credit; fixed 5 of 6 silently-ignored Railway tuning env vars. 506 tests passing at close.

**2026-07-15 — Module 22 complete.** All 22 modules built and tested (463 tests). `register_alert_callback` wired in composition root. `RunMode.PAPER` documented as behaving identically to `RunMode.LIVE` at the orchestration layer (signal-only at every mode).

**2026-07-04 — Modules 1-2 complete.** Configuration Engine + Logging/Error-Handling Framework. Original architecture analysis produced (hexagonal design, module breakdown, DB plan, SRS contradictions flagged).

---

# DESIGN DECISIONS

1. **Hexagonal boundary is load-bearing, not stylistic.** `core/` and `system/` must never import `infrastructure/`. Confirmed still true across the full codebase as of the 2026-07-28 audit.
2. **Built from scratch**, ignoring any previous bot implementation — SRS is the sole source of truth.
3. **Workflow**: implement/verify continuously, no per-module approval gate. A module is only "complete" once its tests are written, actually executed, and passing.
4. **Config**: 4-layer merge (Pydantic field defaults → `defaults.yaml` → active strategy profile YAML → environment variables), explicit env-var mapping (`config/loader.py`'s `_ENV_OVERRIDES`), not magic nested-delimiter parsing.
5. **SRS Rule 5 (no fixed TP/SL) is enforced in code** — a Pydantic validator on `RiskConfig` raises if any profile tries to disable dynamic SL/TP.
6. **Signal-only, forever.** No execution code, no trade-permissioned API keys, anywhere.
7. **Logging**: 7 file-based categories, plain-text rotating files + console handler. `configure_logging()` is explicit, no auto-configuration.
8. **Error handling**: `handle_error()` fans out to registered callbacks for WARNING/ERROR/CRITICAL/FATAL (not INFO); each callback decides for itself which of those it acts on (see `system/error_handler.py`'s docstrings, corrected 2026-07-28).
9. **Database**: SQLite, repository pattern, one connection opened per call with WAL mode, versioned additive migrations (`infrastructure/database/schema.py`'s `MIGRATIONS` tuple — currently v1 initial schema + v2 `tp1_exit_price`). Repositories stay synchronous.
10. **SRS ambiguity resolutions:**
    - "Signal must contain ONLY 6 fields" (Part 4) vs. "12 required Telegram fields" (Part 11): 6 fields = core internal trade state; richer field list = presentation-layer enrichment only.
    - Leverage calculation has no dedicated SRS formula — defined from first principles by this project (confidence-tiered, max 10x, see Module 11), now profile-aware as of 2026-07-28.
    - "No Daily Signal Limit" vs. "Max Active Trades/Sector Exposure": max-active-trades IS enforced (`RiskManagementEngine`); sector exposure is NOT (no sector-classification data source — see KNOWN DEFERRED GAPS).
11. **Turkish-language scope**: SRS requires bot-generated self-analysis/recommendations (Reporting Engine) to be Turkish — implemented, unambiguous. Whether it extends to the live signal/TP/SL/trade-closed messages was an open question; **confirmed 2026-07-28: yes, Turkish, keep as-is.**

---

# CONFIGURATION

- Entry point: `config.loader.get_config()` returns a process-wide `PlatformConfig` singleton (`get_config(force_reload=True)` to rebuild, e.g. in tests).
- Merge order (later wins): Pydantic field defaults → `config/defaults.yaml` → `config/profiles/<STRATEGY_PROFILE>.yaml` → environment variables.
- Active profile via `STRATEGY_PROFILE` env var (`conservative | balanced | aggressive | professional`); unknown name raises `FileNotFoundError` at load time (fails loudly, on purpose).
- Secrets (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `BINANCE_API_KEY`, `BINANCE_API_SECRET`) come only from environment variables, never YAML.
- 14 config categories, each a typed Pydantic sub-model on `PlatformConfig`: `.general .scanner .risk .confidence .bitcoin .market_health .coin_trust .telegram .reports .database .logging .performance .api .security` (+ `.metadata`).
- Railway tuning env vars (all optional, all silently-ignored-until-2026-07-25/28 bug now fully fixed): `MIN_CONFIDENCE`, `MIN_QUOTE_VOLUME_USDT`, `SCAN_INTERVAL_SECONDS`, `MAX_SYMBOLS_TO_ANALYZE`, `SIGNAL_COOLDOWN_MINUTES`, `BINANCE_TIMEOUT_SECONDS` — see `.env.example`.

---

# DATABASE

**Current status: implemented, 15 tables, 2 migrations.** `infrastructure/database/schema.py`'s `MIGRATIONS` tuple: v1 (initial schema — all 15 tables + indexes), v2 (`trades.tp1_exit_price`, added 2026-07-28). `run_migrations(db)` applies anything newer than the current version inside one transaction, tracked in `schema_migrations` — safe to run against an already-deployed Railway database, existing rows get `NULL` for new nullable columns.

- **Connection layer** (`connection.py`): one connection per call (not shared/pooled), WAL mode, explicit transactions.
- **Tables**: `coins`, `coin_profiles`, `coin_statistics`, `signals`, `signal_score_breakdown`, `trades`, `rejections`, `missed_opportunities`, `btc_statistics`, `market_statistics`, `reports`, `filter_performance`, `bot_health`, `error_events`, `config_snapshots`.
- **Repositories** (`repositories/`): one file per related table group, all converting `sqlite3.Row` ↔ `core/models.py` dataclasses.
- Retention: `config.database.retention_policy_days` defaults to `null` (never delete), per SRS.

---

# IMPLEMENTATION RULES
_(mandatory, must never be violated)_

1. **SRS is the single source of truth.**
2. **Never fixed TP/SL** — always dynamic (ATR/structure-derived).
3. **Never hardcode tunable values** — everything goes through `config/`.
4. **Never silently swallow errors** — route real failures through `system/error_handler.handle_error()`.
5. **Never delete historical data** — DB retention defaults to unlimited.
6. **Signal-only, forever** — no execution code, no trade-permissioned API keys.
7. **Don't redesign the approved architecture** without a genuine technical reason; don't rewrite completed/approved modules unnecessarily.
8. **A module/fix is only "complete" once its tests are written, actually executed, and passing.**
9. **Never invent an SRS formula the spec doesn't define** — document the gap instead (see KNOWN DEFERRED GAPS).
10. **Before deleting or overwriting any file that wasn't created in the current turn, open and read it first.**

---

# CODING STANDARDS

- **Environment:** Python 3.12. `from __future__ import annotations` at the top of every module.
- **Pydantic** only in `config/`. **Plain dataclasses** in `core/` — dependency-light, framework-free domain layer.
- **Repository pattern** for all database access — no raw SQL outside `infrastructure/database/`.
- Every tunable value belongs in `config/schema.py` + `defaults.yaml`; no magic numbers inside engine code.
- Docstrings cite the specific SRS Part/Rule (or explicitly say "no SRS formula, defined here") motivating each non-obvious design choice.
- **Every module has a matching `tests/unit/test_*.py`**, executed before the module/fix is considered done.
- One file per concern; avoid god-files.
- Dependency direction is strictly one-way: `infrastructure/`/`application/` may depend on `core/`; `core/`/`system/` never depend on `infrastructure/`.
- `pip install -r requirements.txt --break-system-packages` in this sandboxed environment (Debian/Ubuntu externally-managed Python).

---

# OPERATIONAL STATUS

The platform is feature-complete for its SRS scope and ready for the paper/live-signal testing phase. No further modules are planned; future work is either (a) fixing a real bug found during testing, or (b) a deliberately-scoped new feature request (e.g. one of the KNOWN DEFERRED GAPS above, or a new indicator) — not open-ended re-implementation. If a future session finds this document's SESSION LOG doesn't match the actual filesystem, trust the filesystem and update this document — do not silently work around the mismatch.
