# Trading Bot - Binance Testnet

Bot de trading autónomo para Binance Testnet, construido en fases siguiendo prácticas de ingeniería de software financiero.

## 🚀 Setup Rápido (Desarrollo Local)

### 1. Requisitos Previos
- Python 3.11+
- Git

### 2. Clonar y Configurar Entorno
```bash
cd TradingBot
python -m venv .venv
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # Linux/Mac
pip install -e ".[dev,sqlite]"  # dev ya incluye aiosqlite, sqlite extra es alias; para Postgres añade ,postgres
# Alternativa mínima SQLite: pip install -e ".[dev,sqlite]"
```

### 3. Variables de Entorno
```bash
copy .env.example .env
# Edita .env con tus valores reales (ver sección "API Keys de Binance Testnet")
```

### 4. Base de Datos (SQLite para desarrollo)
No requiere instalación adicional. El archivo `trading_bot.db` se crea automáticamente.
#### Migraciones con Alembic
```bash
alembic upgrade head
```

### 5. Correr Tests
```bash
pytest
```

---

## 🔑 API Keys de Binance Testnet

1. Ve a **https://testnet.binance.vision/**
2. Inicia sesión con GitHub/Google o crea cuenta
3. Ve a **API Management** → **Create API Key**
4. Nombra la key (ej. "trading-bot-dev")
5. **IMPORTANTE**: Habilita solo:
   - ✅ Spot & Margin Trading
   - ✅ Read Info
   - ❌ **NO** habilites Withdrawals
6. Copia **API Key** y **Secret Key** a tu `.env`:
   ```env
   BINANCE_API_KEY=tu_api_key_aqui
   BINANCE_API_SECRET=tu_secret_key_aqui
   ```

---

## 📁 Estructura del Proyecto

```
TradingBot/
├── src/trading_bot/
│   ├── bot.py               # Loop principal Fase 4 (orquestador resiliente)
│   ├── config/              # Configuración y settings (pydantic)
│   ├── data_collection/     # Binance ccxt + WebSocket + histórico
│   ├── analysis/            # Indicadores (pandas-ta) + régimen
│   ├── decision/            # Estrategias puras + signals
│   ├── risk_management/     # Límites, sizing ATR, circuit breakers, kill switch
│   ├── execution/           # OrderExecutor con gateway obligatorio + executed_orders
│   ├── storage/             # SQLAlchemy + Alembic
│   ├── notifications/       # Telegram bot (alertas + kill switch remoto)
│   └── backtesting/         # vectorbt engine
├── scripts/
│   ├── ingest_historical.py # 30 días histórico a BD
│   ├── run_backtest.py      # Backtests sobre BD
│   ├── run_bot.py           # Entrypoint Fase 4
│   └── dashboard.py         # Reporte métricas §9
├── tests/
├── alembic/
├── Dockerfile
├── docker-compose.yml
└── README.md
```

---

## 🧪 Tests

```bash
pytest
pytest tests/test_risk_management.py -v --cov=src/trading_bot/risk_management --cov-report=term-missing
pytest tests/test_execution_phase4.py -v
```

Fase 3 tiene 25 tests con 93% cobertura y todos los casos límite (§6) + verificación de que NINGUNA orden pasa sin `RiskEngine`.

---

## 📦 Despliegue Fase 4 - Docker + Testnet 24/7 en VPS (Hetzner/DigitalOcean)

**Solo Testnet - dinero ficticio.**

### Local con Docker Compose (PostgreSQL)
```bash
# 1. Configura .env (keys de TESTNET, nunca mainnet)
cp .env.example .env
# Edita BINANCE_API_KEY, BINANCE_API_SECRET, TELEGRAM_BOT_TOKEN

# 2. Levanta bot + postgres (migración automática)
docker compose up --build -d
docker compose logs -f bot

# 3. Ver métricas
docker compose exec bot python scripts/dashboard.py

# 4. Parar
docker compose down
```

`docker-compose.yml` levanta `db` (postgres:15-alpine) y `bot` (python:3.11-slim). `DATABASE_URL` se sobrescribe a `postgresql+asyncpg://trader:traderpass@db:5432/trading_bot`. Si vienes de SQLite, la migración a Postgres es automática vía `DATABASE_URL` (SQLAlchemy) + `alembic upgrade head`.

### VPS
```bash
# En el VPS (Ubuntu 22.04)
sudo apt update && sudo apt install docker.io docker-compose-plugin git -y
git clone <repo> TradingBot && cd TradingBot
cp .env.example .env && nano .env  # pon keys testnet
docker compose up --build -d
# Logs
docker compose logs -f --tail=100 bot
# Dashboard
docker compose exec bot python scripts/dashboard.py
```

**Validación Testnet estricta en código** (`execution/executor.py:44` y `bot.py:18`): si `BINANCE_BASE_URL` no contiene `testnet` o `TRADING_MODE` no es `testnet/paper`, el bot se niega a arrancar. Si la key no es de testnet, no operará.

### Loop Principal
`src/trading_bot/bot.py:22` orquesta: recolección → análisis → decisión → riesgo → ejecución → registro. Errores por símbolo no tumban el loop. `scripts/run_bot.py` maneja SIGINT/SIGTERM. Poll cada `min(60, timeframe/2)`.

### Notificaciones Telegram
Cada orden ejecutada, circuit breaker, límite diario y max drawdown notifican. Comandos remotos: `/status`, `/kill` (con confirmación inline), `/unkill` (requiere usuario autorizado).

### Dashboard Mínimo
```bash
python scripts/dashboard.py
pytest scripts/run_backtest.py  # backtest 6 meses
```

---

## Fase 5 - Filtro Noticias/Sentimiento (Opcional, menor prioridad)

Solo como **filtro que reduce o veta**, nunca genera señal.

```bash
# Activa en .env
SENTIMENT_ENABLED=true
CRYPTOPANIC_TOKEN=tu_token_opcional  # funciona sin token en public=true
# Ajusta umbrales (opcional)
SENTIMENT_VETO_THRESHOLD=-0.6
SENTIMENT_REDUCE_THRESHOLD=-0.3
# Evento macro FOMC -> pausa automática 2h antes/después
MACRO_EVENTS_JSON='[{"name":"FOMC","time":"2026-09-17T18:00:00Z","before":2,"after":2}]'
```

Lógica en `sentiment/filter.py:22`: si `tone_score <= -0.6` y `relevancia >=0.8` → veto (`SENTIMENT_VETO`), si `<= -0.3` y `relevancia >=0.6` → reduce 50% (`SENTIMENT_REDUCE`). Todo queda auditado en `risk_logs` y `executed_orders`. Ver `tests/test_sentiment_phase5.py` (14 tests).

---

## Fase 6 - Real con dinero real (SOLO con autorización explícita por escrito)

> **No ejecutes esta fase hasta que yo lo confirme por escrito y solo después de meses de Testnet consistente.**

### Configuración separada y diferenciada (nunca reutilizar .env de testnet)

```bash
cp .env.production.example .env.production
nano .env.production  # pon keys MAINNET, con retiros OFF e IP whitelist
# Verifica en Binance: API Management -> Restrictions -> Enable Withdrawals OFF
# y "Restrict access to trusted IPs only" con IP de tu VPS
```

`.env.production.example:1` y `docker-compose.prod.yml:1` usan BD separada `trading_bot_prod` y `TRADING_MODE=live`.

### Triple confirmación manual (no viene por defecto en ningún template)

El bot **se niega a arrancar** en live sin estas 3 vars creadas a mano:

```bash
ALLOW_LIVE_TRADING=true
CONFIRM_LIVE_TRADING_I_UNDERSTAND_RISK=true
LIVE_TRADING_CONFIRM_PHRASE="SI, ENTIENDO QUE PUEDO PERDER TODO Y AUTORIZO TRADING REAL"
```

Validadas en `config/production.py:12` `assert_live_trading_authorized()`. Además verifica que `BINANCE_BASE_URL=https://api.binance.com` y que los límites sean más conservadores que Testnet (ej. `RISK_MAX_DAILY_LOSS_PCT<=0.015`, `POSITION_RISK<=0.005`, `EXPOSURE<=0.10`).

### Verificación API keys restricciones

Al iniciar en live, `verify_api_key_restrictions()` llama `sapiGetApiRestrictions` y bloquea si `canWithdraw==true`. IP whitelist validada si `LIVE_ALLOWED_IPS` está seteada.

### Despliegue prod

```bash
docker compose -f docker-compose.prod.yml --env-file .env.production up --build -d
docker compose -f docker-compose.prod.yml logs -f bot
```

Monitoreo reforzado: `LIVE_ALERT_EVERY_TRADE=true` notifica cada operación, y `risk_status` más frecuente las primeras semanas.

Cada paso requiere tu revisión manual individual, no se activa automático.

---

## ⚠️ Reglas de Seguridad Críticas

1. **NUNCA** uses mainnet keys en este repositorio
2. **NUNCA** commitees archivo `.env` real
3. **SIEMPRE** verifica que `BINANCE_BASE_URL=https://testnet.binance.vision`
4. El bot **NO** operará en mainnet sin autorización escrita explícita (Fase 6)
5. API keys de Testnet deben tener **Withdrawals DESHABILITADO**
6. Fase 3 es la más crítica: no avances a real sin aprobarla
