# Altcoin Sniper MVP

Sistema de monitoreo automatizado que detecta altcoins recién listadas mediante dos fuentes en paralelo — **CoinGecko** y **GeckoTerminal** — evalúa su seguridad con GoPlus Security API y envía una alerta a Telegram por **cada token detectado**, indicando claramente si es un posible scam o un token legítimo.

## Setup rápido

```bash
# 1. Clonar e instalar
pip install -r requirements.txt

# 2. Configurar
cp .env.example .env
# Editar .env con tus credenciales

# 3. Crear la base de datos
createdb altcoin_sniper

# 4. Ejecutar
python main.py
```

## Obtener credenciales

- **CoinGecko API key**: registro en [coingecko.com/en/api](https://www.coingecko.com/en/api) → plan Demo (gratuito).
- **Telegram bot**: hablar con [@BotFather](https://t.me/BotFather) → `/newbot` → copiar el token devuelto.
- **Telegram chat_id**: agregar el bot a un canal/grupo (o iniciar conversación directa) → usar [@userinfobot](https://t.me/userinfobot) para obtener el `chat_id`.
- **GoPlus** (opcional): funciona sin API key. Con key obtienes mayor rate limit en [gopluslabs.io](https://gopluslabs.io).

## Variables de entorno

| Variable | Obligatoria | Default | Descripción |
|---|---|---|---|
| `COINGECKO_API_KEY` | sí | — | API key de CoinGecko (header `x-cg-demo-api-key`) |
| `DATABASE_URL` | sí | — | DSN PostgreSQL (`postgresql://user:pass@host:port/db`) |
| `TELEGRAM_TOKEN` | sí | — | Token del bot de Telegram |
| `TELEGRAM_CHAT_ID` | sí | — | Chat ID destino (canal, grupo o usuario) |
| `GOPLUS_API_KEY` | no | vacío | API key opcional para GoPlus Security |
| `POLL_INTERVAL_SECONDS` | no | `300` | Frecuencia del ciclo de polling en segundos |
| `MIN_SECURITY_SCORE` | no | `60` | Umbral del score para clasificar un token como "seguro" vs "scam" |
| `MAX_SELL_TAX` | no | `0.10` | Sell tax máximo antes de penalizar el score (-30 puntos) |
| `MAX_BUY_TAX` | no | `0.10` | Buy tax máximo antes de penalizar el score (-20 puntos) |

> Si `TELEGRAM_TOKEN` o `TELEGRAM_CHAT_ID` están vacíos, las alertas se imprimen en stdout con el prefijo `[TELEGRAM SIMULADO]` (modo desarrollo).

## Fuentes de descubrimiento

El sistema consulta **dos fuentes en paralelo** cada ciclo y fusiona los resultados, deduplicando por `(address, chain_id)`:

| Fuente | Endpoint | Rol |
|---|---|---|
| **CoinGecko** | `GET /coins/list?include_platform=true` | Discovery de tokens listados en CoinGecko |
| **GeckoTerminal** | `GET /networks/new_pools` | Discovery de pools nuevos en DEXes (detección más temprana) |
| **CoinGecko** | `GET /coins/{id}` | Market data (precio, FDV, volumen, links) |

> GeckoTerminal es gratuito y no requiere API key (~10 req/min). CoinGecko requiere API key en el plan Demo (gratuito).

Ambas fuentes aplican un **primer ciclo de baseline**: al arrancar, solo memorizan el estado actual y no disparan alertas. A partir del segundo ciclo detectan tokens/pools verdaderamente nuevos. Esto evita un aluvión de alertas al iniciar el sistema.

Si una fuente falla (error de red, rate limit), la otra continúa sin interrumpir el ciclo.

## Comportamiento de alertas

**Todos los tokens detectados generan una alerta en Telegram**, sin filtrar ninguno. El sistema distingue dos tipos de mensaje:

### Token legítimo (score ≥ MIN_SECURITY_SCORE)

```
🟢 Nuevo Token Detectado

*AlphaToken* (`ALPHA`)
Chain: `base`
Fuente: `CoinGecko`
Contrato: `0xAbCd...1234`

*Score de Seguridad: 85/100*

📊 Mercado
• Precio: $0.00042
• Market Cap: $420,000.00
• FDV: $1,200,000.00
• FDV/MC: 2.9x
• Volumen 24h: $85,000.00

🔐 Seguridad
• Honeypot: ✅ No
• Sell Tax: 2.0%
• Buy Tax: 2.0%
• Mintable: ✅ No
• Blacklist: ✅ No
• Open Source: ✅ Sí

⚠️ Alertas detectadas
• Ninguna

🔗 Web | Twitter | Telegram

_Análisis automatizado. No es consejo financiero._
```

### Token riesgoso / SCAM (score < MIN_SECURITY_SCORE)

```
🔴 ⚠️ SCAM DETECTADO — Token Riesgoso

*RugToken* (`RUG`)
Chain: `ethereum`
Fuente: `GeckoTerminal`
Contrato: `0xDead...BEEF`

*Score de Seguridad: 0/100*

📊 Mercado
• Datos de mercado no disponibles

🔐 Seguridad
• Honeypot: ❌ Sí
• Sell Tax: N/A
• Buy Tax: N/A
• Mintable: ❌ Sí
• Blacklist: ❌ Sí
• Open Source: ⚠️ No

⚠️ Alertas detectadas
• Honeypot detectado

_Análisis automatizado. No es consejo financiero._
```

### Escala de colores del score

| Emoji | Rango | Significado |
|---|---|---|
| 🟢 | 80–100 | Seguro |
| 🟡 | 65–79 | Aceptable |
| 🟠 | 30–64 | Riesgoso |
| 🔴 | 0–29 / SCAM | Peligroso / Scam |

## Sistema de scoring (`security.py`)

- **Score 0 inmediato**: honeypot, `sell_tax > 50%`, owner oculto, error de API.
- **Penalizaciones** desde 100:

| Condición | Penalización |
|---|---|
| `sell_tax > MAX_SELL_TAX` | -30 |
| `buy_tax > MAX_BUY_TAX` | -20 |
| `can_take_back_ownership` | -25 |
| `is_mintable` | -25 |
| `has_blacklist` | -20 |
| `slippage_modifiable` | -20 |
| `transfer_pausable` | -15 |
| `is_open_source = false` | -15 |
| `is_proxy` | -10 |

## Estructura del proyecto

```
altcoin_sniper/
├── .env                ← variables de entorno (NO commitear)
├── .env.example        ← plantilla de variables
├── requirements.txt    ← dependencias Python
├── README.md           ← este archivo
│
├── config.py           ← configuración central (lee .env)
├── database.py         ← schema PostgreSQL + operaciones CRUD async
├── security.py         ← cliente GoPlus + scoring de seguridad
├── fetcher.py          ← discovery: CoinGecko + GeckoTerminal; market data: CoinGecko
├── alerts.py           ← formateador y enviador de alertas Telegram
└── main.py             ← loop principal de polling + orquestación
```

| Archivo | Responsabilidad |
|---|---|
| `config.py` | Único punto de verdad para variables de entorno. Valida obligatorias al importarse. |
| `database.py` | Inicializa el pool asyncpg, crea el schema (`tokens`, `security_checks`, `alerts_sent`) y expone CRUD asíncrono. |
| `security.py` | Consulta GoPlus Security API y calcula el score (auto-reject + penalizaciones). |
| `fetcher.py` | Discovery desde CoinGecko (`/coins/list`) y GeckoTerminal (`/networks/new_pools`) en paralelo. Enriquecimiento de mercado via CoinGecko. Ambas fuentes aplican baseline en el primer ciclo para evitar alertas masivas al arrancar. |
| `alerts.py` | Formatea el mensaje Markdown y lo envía a Telegram (o stdout si no está configurado). |
| `main.py` | Orquesta el ciclo de polling con `asyncio.Semaphore(5)`, captura señales `SIGINT`/`SIGTERM` y loggea estadísticas. |
