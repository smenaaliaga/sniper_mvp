# SPEC: Altcoin Sniper MVP

## Propósito de este documento

Este documento es la especificación técnica completa para implementar el **Altcoin Sniper MVP** usando Claude Code. Contiene todo lo necesario para que Claude Code construya, conecte y ejecute el sistema sin ambigüedad: estructura de archivos, contratos de cada módulo, esquema de base de datos, flujo de datos, comportamiento esperado, casos de error y criterios de aceptación.

---

## 1. Visión general del sistema

El MVP es un sistema de monitoreo automatizado que detecta altcoins nuevas, evalúa su seguridad automáticamente y envía alertas cuando un token pasa los filtros mínimos de calidad.

### 1.1 Qué hace el MVP

```
CADA 5 MINUTOS:
  1. Consulta CoinGecko por tokens recientemente añadidos
  2. Para cada token nuevo no procesado anteriormente:
     a. Consulta GoPlus Security API → obtiene flags de seguridad
     b. Calcula un score de seguridad (0–100)
     c. Si score >= 60: consulta datos de mercado en CoinGecko
     d. Si pasa todos los filtros: envía alerta a Telegram
     e. Guarda resultado en PostgreSQL
```

### 1.2 Qué NO hace el MVP

- No ejecuta transacciones ni compras
- No tiene interfaz web ni dashboard
- No usa modelos ML (eso es Fase 3)
- No usa WebSocket (eso es Fase 2)
- No analiza el código del contrato (eso lo hace GoPlus)

### 1.3 Stack tecnológico

```
Lenguaje:        Python 3.11+
Runtime:         asyncio (todo el sistema es async)
Base de datos:   PostgreSQL 15+
HTTP client:     aiohttp
DB driver:       asyncpg
Alertas:         Telegram Bot API
OS:              Windows
```

---

## 2. Estructura de archivos

```
altcoin_sniper/
├── .env                    ← variables de entorno (NO commitear)
├── .env.example            ← plantilla de variables
├── requirements.txt        ← dependencias Python
├── README.md               ← instrucciones de setup y ejecución
│
├── config.py               ← configuración central (lee .env)
├── database.py             ← schema PostgreSQL + operaciones CRUD async
├── security.py             ← cliente GoPlus + scoring de seguridad
├── fetcher.py              ← cliente CoinGecko REST API
├── alerts.py               ← formateador y enviador de alertas Telegram
└── main.py                 ← loop principal de polling + orquestación
```

Claude Code debe crear exactamente estos archivos en el directorio de trabajo. No crear subdirectorios para el MVP.

---

## 3. Variables de entorno

### 3.1 Archivo `.env.example`

```bash
# ── OBLIGATORIAS ──────────────────────────────────────
# CoinGecko: obtener en https://www.coingecko.com/en/api
COINGECKO_API_KEY=CG-xxxxxxxxxxxxxxxxxxxxxxxxxxxx

# PostgreSQL: ajustar según instalación local
DATABASE_URL=postgresql://postgres:password@localhost:5432/altcoin_sniper

# Telegram: crear bot con @BotFather, obtener chat_id con @userinfobot
TELEGRAM_TOKEN=1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_CHAT_ID=-1001234567890

# ── OPCIONALES ────────────────────────────────────────
# GoPlus funciona sin API key (gratis), pero con key tiene más rate limit
GOPLUS_API_KEY=

# Intervalo de polling en segundos (default: 300 = 5 minutos)
POLL_INTERVAL_SECONDS=300

# Score mínimo para enviar alerta (default: 60)
MIN_SECURITY_SCORE=60

# Liquidez mínima en USD para considerar el token (default: 50000)
MIN_LIQUIDITY_USD=50000
```

### 3.2 Reglas de carga

- Usar `python-dotenv` para cargar `.env` automáticamente al iniciar
- Si una variable obligatoria falta, el sistema debe fallar con mensaje claro
- Los valores numéricos deben convertirse a sus tipos correctos (int, float)

---

## 4. Módulo `config.py`

### 4.1 Responsabilidad

Único punto de verdad para toda la configuración. Todos los demás módulos importan desde aquí, nunca leen `os.environ` directamente.

### 4.2 Interfaz esperada

```python
# Uso en cualquier módulo:
from config import config

config.COINGECKO_API_KEY    # str
config.DATABASE_URL         # str
config.TELEGRAM_TOKEN       # str
config.TELEGRAM_CHAT_ID     # str
config.GOPLUS_API_KEY       # str (puede ser vacío)
config.POLL_INTERVAL_SECONDS  # int, default 300
config.MIN_SECURITY_SCORE   # float, default 60.0
config.MIN_LIQUIDITY_USD    # float, default 50_000.0
config.MAX_SELL_TAX         # float, default 0.10
config.MAX_BUY_TAX          # float, default 0.10

# Mapa de chain IDs para GoPlus
config.SUPPORTED_CHAINS     # dict: {"ethereum": "1", "binance-smart-chain": "56", ...}
```

### 4.3 Validación al iniciar

Al importar `config`, debe validar que `DATABASE_URL`, `TELEGRAM_TOKEN` y `TELEGRAM_CHAT_ID` no estén vacíos. Si falta alguno, lanzar `ValueError` con mensaje que indique cuál variable falta.

---

## 5. Módulo `database.py`

### 5.1 Responsabilidad

Gestionar la conexión a PostgreSQL y todas las operaciones de lectura/escritura. Usar `asyncpg` con connection pool.

### 5.2 Schema SQL

Claude Code debe crear este schema exacto al inicializar:

```sql
CREATE TABLE IF NOT EXISTS tokens (
    id              SERIAL PRIMARY KEY,
    address         VARCHAR(42) NOT NULL,
    chain_id        VARCHAR(20) NOT NULL,
    name            VARCHAR(200),
    symbol          VARCHAR(20),
    coingecko_id    VARCHAR(200),
    first_seen_at   TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(address, chain_id)
);

CREATE TABLE IF NOT EXISTS security_checks (
    id                          SERIAL PRIMARY KEY,
    token_address               VARCHAR(42) NOT NULL,
    chain_id                    VARCHAR(20) NOT NULL,
    checked_at                  TIMESTAMPTZ DEFAULT NOW(),

    -- Flags de GoPlus (NULL = no se pudo obtener)
    is_honeypot                 BOOLEAN,
    sell_tax                    NUMERIC(5,4),
    buy_tax                     NUMERIC(5,4),
    hidden_owner                BOOLEAN,
    can_take_back_ownership     BOOLEAN,
    is_mintable                 BOOLEAN,
    has_blacklist               BOOLEAN,
    transfer_pausable           BOOLEAN,
    is_open_source              BOOLEAN,
    is_proxy                    BOOLEAN,
    slippage_modifiable         BOOLEAN,
    owner_address               VARCHAR(42),

    -- Datos de mercado
    liquidity_usd               NUMERIC(20,2),
    market_cap_usd              NUMERIC(20,2),
    fdv_usd                     NUMERIC(20,2),
    volume_24h_usd              NUMERIC(20,2),

    -- Resultado del análisis
    security_score              NUMERIC(5,2) NOT NULL,
    passed_filters              BOOLEAN NOT NULL,
    reject_reason               TEXT,

    -- Error si la API falló
    api_error                   TEXT
);

CREATE TABLE IF NOT EXISTS alerts_sent (
    id              SERIAL PRIMARY KEY,
    token_address   VARCHAR(42) NOT NULL,
    chain_id        VARCHAR(20) NOT NULL,
    sent_at         TIMESTAMPTZ DEFAULT NOW(),
    security_score  NUMERIC(5,2),
    token_name      VARCHAR(200),
    token_symbol    VARCHAR(20)
);

-- Índices
CREATE INDEX IF NOT EXISTS idx_tokens_address_chain
    ON tokens(address, chain_id);

CREATE INDEX IF NOT EXISTS idx_security_address_chain
    ON security_checks(token_address, chain_id);

CREATE INDEX IF NOT EXISTS idx_alerts_address_chain
    ON alerts_sent(token_address, chain_id);
```

### 5.3 Funciones públicas requeridas

```python
async def setup_db() -> None:
    """
    Inicializa el connection pool y crea el schema si no existe.
    Llamar una sola vez al arrancar el sistema.
    Lanzar excepción si la conexión falla.
    """

async def save_token(
    address: str,
    chain_id: str,
    name: str,
    symbol: str,
    coingecko_id: str | None = None
) -> None:
    """
    Inserta un token nuevo. Si ya existe (address + chain_id), no hace nada.
    """

async def save_security_check(data: dict) -> None:
    """
    Guarda el resultado completo del análisis de seguridad.
    El dict debe contener todas las claves del schema security_checks.
    """

async def was_checked(address: str, chain_id: str) -> bool:
    """
    Retorna True si el token ya tiene un registro en security_checks.
    Usado para evitar analizar el mismo token dos veces.
    """

async def was_alerted(address: str, chain_id: str) -> bool:
    """
    Retorna True si ya se envió una alerta para este token.
    """

async def save_alert(
    address: str,
    chain_id: str,
    score: float,
    name: str,
    symbol: str
) -> None:
    """
    Registra que se envió una alerta para este token.
    """

async def get_stats() -> dict:
    """
    Retorna estadísticas del sistema para logging:
    {
        "total_tokens_seen": int,
        "total_checked": int,
        "total_passed": int,
        "total_alerts_sent": int,
        "scams_detected": int
    }
    """
```

### 5.4 Manejo de conexión

- El pool debe crearse una sola vez (singleton) en `setup_db()`
- `min_size=2`, `max_size=10`
- Si la conexión cae, asyncpg reintentará automáticamente
- No exponer el pool directamente: todas las operaciones deben ir por las funciones públicas

---

## 6. Módulo `security.py`

### 6.1 Responsabilidad

Consultar GoPlus Security API y calcular un score de seguridad para cada token.

### 6.2 Endpoint GoPlus

```
GET https://api.gopluslabs.io/api/v1/token_security/{chain_id}
    ?contract_addresses={address}

Headers:
    Authorization: Bearer {GOPLUS_API_KEY}  (solo si la key está configurada)

Timeout: 10 segundos
```

### 6.3 Función principal

```python
async def check_token(address: str, chain_id: str) -> dict:
    """
    Consulta GoPlus y retorna un dict con el resultado completo.

    Siempre retorna un dict con todas las claves, nunca lanza excepción.
    Si la API falla, retorna el dict con passed_filters=False
    y api_error con la descripción del error.

    Claves del dict retornado:
        address: str
        chain_id: str
        is_honeypot: bool | None
        sell_tax: float | None
        buy_tax: float | None
        hidden_owner: bool | None
        can_take_back_ownership: bool | None
        is_mintable: bool | None
        has_blacklist: bool | None
        transfer_pausable: bool | None
        is_open_source: bool | None
        is_proxy: bool | None
        slippage_modifiable: bool | None
        owner_address: str | None
        liquidity_usd: float           (inicialmente 0, se actualiza después)
        market_cap_usd: float
        fdv_usd: float
        volume_24h_usd: float
        security_score: float          (0–100)
        passed_filters: bool
        reject_reason: str | None
        api_error: str | None
    """
```

### 6.4 Lógica de scoring

El score se calcula en dos pasadas:

**Pasada 1 — Auto-reject (score = 0, passed = False):**

| Condición | Razón de rechazo |
|---|---|
| `is_honeypot == True` | "Honeypot detectado" |
| `sell_tax > 0.50` | "Sell tax crítico: {valor}%" |
| `hidden_owner == True` | "Owner oculto" |
| `api_error is not None` | "Error de API: {error}" |

Si ninguna condición de auto-reject se cumple, continuar con Pasada 2.

**Pasada 2 — Score desde 100, restar penalizaciones:**

| Condición | Penalización |
|---|---|
| `sell_tax > MAX_SELL_TAX (0.10)` | -30 puntos |
| `buy_tax > MAX_BUY_TAX (0.10)` | -20 puntos |
| `can_take_back_ownership == True` | -25 puntos |
| `is_mintable == True` | -25 puntos |
| `has_blacklist == True` | -20 puntos |
| `slippage_modifiable == True` | -20 puntos |
| `transfer_pausable == True` | -15 puntos |
| `is_open_source == False` | -15 puntos |
| `is_proxy == True` | -10 puntos |

Score mínimo: 0. Score máximo: 100.

`passed_filters = score >= config.MIN_SECURITY_SCORE`

`reject_reason`: concatenar todas las penalizaciones aplicadas separadas por "; ". Si no hay penalizaciones, `None`.

### 6.5 Parsing de la respuesta GoPlus

GoPlus retorna valores como strings "0" o "1" para booleans, y strings decimales para taxes. El parsing debe:

- Convertir "1" → `True`, "0" → `False`, `None`/ausente → `None`
- Convertir "0.05" → `0.05` float, con manejo de excepción si no es parseable
- El address en la respuesta viene en lowercase: `data["result"][address.lower()]`
- Si el address no está en result, considerar como token no encontrado (api_error)

---

## 7. Módulo `fetcher.py`

### 7.1 Responsabilidad

Obtener tokens nuevos y datos de mercado desde CoinGecko REST API.

### 7.2 Endpoints a usar

```
# Tokens recientemente añadidos
GET https://api.coingecko.com/api/v3/coins/list/new

# Datos de mercado de un token específico
GET https://api.coingecko.com/api/v3/coins/{id}
    ?localization=false
    &tickers=false
    &market_data=true
    &community_data=false
    &developer_data=true
    &sparkline=false

Headers:
    x-cg-demo-api-key: {COINGECKO_API_KEY}  (si está configurada)
    accept: application/json
```

### 7.3 Funciones requeridas

```python
async def get_recently_added() -> list[dict]:
    """
    Obtiene tokens recientemente añadidos a CoinGecko.

    Filtra solo los tokens que tienen contract address conocido
    en alguna de las chains de config.SUPPORTED_CHAINS.

    Retorna lista de dicts:
    [
        {
            "coingecko_id": str,   # ej: "pepe-2"
            "name": str,
            "symbol": str,         # siempre uppercase
            "address": str,        # siempre lowercase
            "chain_id": str,       # ej: "1" para Ethereum
            "platform": str,       # ej: "ethereum"
        },
        ...
    ]

    Si hay error de red o rate limit (HTTP 429), retornar lista vacía.
    Si hay rate limit, esperar 60 segundos antes de retornar.
    """

async def get_market_data(coingecko_id: str) -> dict | None:
    """
    Obtiene datos de mercado para un token específico.

    Retorna dict:
    {
        "price_usd": float,
        "market_cap_usd": float,
        "fdv_usd": float,
        "volume_24h_usd": float,
        "liquidity_usd": float,      # approximación via market_cap si no hay dato directo
        "price_change_24h": float,
        "github_stars": int,
        "github_commits_4w": int,
        "links": {
            "homepage": str | None,
            "twitter": str | None,   # solo el username, sin URL
            "telegram": str | None,  # solo el channel name
            "github": str | None,    # URL completa del primer repo
        }
    }

    Retornar None si el token no existe o hay error.
    """
```

### 7.4 Chains soportadas

```python
SUPPORTED_CHAINS = {
    "ethereum":              "1",
    "binance-smart-chain":   "56",
    "polygon-pos":           "137",
    "base":                  "8453",
    "arbitrum-one":          "42161",
    "avalanche":             "43114",
    "optimistic-ethereum":   "10",
}
```

Solo incluir tokens cuya platform esté en este mapa Y cuya address tenga más de 10 caracteres.

### 7.5 Rate limiting

CoinGecko free tier: 30 requests/minuto. Si se recibe HTTP 429:
- Log warning con mensaje claro
- Esperar 60 segundos
- Retornar lista vacía (no reintentar en el mismo ciclo)

---

## 8. Módulo `alerts.py`

### 8.1 Responsabilidad

Formatear y enviar mensajes a Telegram.

### 8.2 Funciones requeridas

```python
async def send_token_alert(
    token: dict,
    security: dict,
    market: dict | None
) -> bool:
    """
    Envía alerta de token nuevo que pasó los filtros.
    Retorna True si el envío fue exitoso.
    """

async def send_system_message(text: str) -> bool:
    """
    Envía mensaje del sistema (errores, stats, inicio/parada).
    """
```

### 8.3 Formato del mensaje de alerta

El mensaje debe ser en Markdown compatible con Telegram (MarkdownV2 o Markdown básico). Estructura exacta:

```
🟢 *Nuevo Token Detectado*  (🟢 si score>=80, 🟡 si >=65, 🟠 si >=60)

*NOMBRE* (`SYMBOL`)
Chain: `nombre_chain`
Contrato: `0xaddress...`

*Score de Seguridad: XX/100*

📊 *Mercado*
• Precio: $X.XXXX
• Market Cap: $X,XXX,XXX
• FDV: $X,XXX,XXX
• FDV/MC: X.Xx
• Volumen 24h: $X,XXX,XXX

🔐 *Seguridad*
• Honeypot: ✅ No  (o ❌ Sí)
• Sell Tax: X.X%
• Buy Tax: X.X%
• Mintable: ✅ No  (o ❌ Sí)
• Blacklist: ✅ No  (o ❌ Sí)
• Open Source: ✅ Sí  (o ⚠️ No)

⚠️ *Alertas detectadas*
• [razones de penalización, una por línea, o "Ninguna"]

🔗 Web | Twitter | Telegram | GitHub
(solo los links disponibles)

_Análisis automatizado. No es consejo financiero._
```

### 8.4 Comportamiento cuando Telegram no está configurado

Si `TELEGRAM_TOKEN` o `TELEGRAM_CHAT_ID` están vacíos, imprimir el mensaje completo en stdout con prefijo `[TELEGRAM SIMULADO]` y retornar `True`. Esto permite correr el sistema en modo desarrollo sin Telegram.

### 8.5 Manejo de errores

- Timeout: 10 segundos
- Si falla, log el error y retornar `False`
- No relanzar excepciones

---

## 9. Módulo `main.py`

### 9.1 Responsabilidad

Orquestación del loop principal de polling.

### 9.2 Flujo del ciclo de polling

```python
async def run_cycle() -> dict:
    """
    Ejecuta un ciclo completo de polling.
    Retorna dict con estadísticas del ciclo:
    {
        "tokens_found": int,
        "already_checked": int,
        "passed_security": int,
        "alerts_sent": int,
        "errors": int,
        "duration_seconds": float,
    }
    """
```

El ciclo debe:

1. Llamar `fetcher.get_recently_added()`
2. Para cada token en la lista, verificar con `database.was_checked()`
3. Procesar los no-chequeados en paralelo con `asyncio.gather()` y `asyncio.Semaphore(5)`
4. Para cada token a procesar:
   - `database.save_token()`
   - `security.check_token()` → obtiene resultado de seguridad
   - `database.save_security_check()` → guarda siempre, pase o no
   - Si `passed_filters=True` y `liquidity OK` y `not was_alerted()`:
     - `fetcher.get_market_data()` (solo si tenemos coingecko_id)
     - Verificar liquidez mínima
     - `alerts.send_token_alert()`
     - `database.save_alert()`
5. Loggear resumen del ciclo

### 9.3 Loop principal

```python
async def main():
    # 1. Cargar configuración y validar
    # 2. Inicializar DB (setup_db)
    # 3. Enviar mensaje de inicio a Telegram
    # 4. Loop infinito:
    #    a. Registrar tiempo de inicio
    #    b. run_cycle()
    #    c. Loggear stats
    #    d. Cada 10 ciclos: loggear stats acumuladas de DB
    #    e. Dormir POLL_INTERVAL_SECONDS
    #    f. Capturar excepciones sin detener el loop
```

### 9.4 Logging

Usar el módulo `logging` estándar de Python:
- Nivel INFO para operaciones normales
- Nivel WARNING para tokens descartados y rate limits
- Nivel ERROR para excepciones
- Formato: `%(asctime)s [%(levelname)s] %(message)s` con `datefmt="%H:%M:%S"`

Mensajes de log obligatorios:
```
INFO  "🚀 Altcoin Sniper MVP iniciando..."
INFO  "✅ Base de datos inicializada"
INFO  "🔄 Ciclo #{n} iniciando — {timestamp}"
INFO  "  📋 {n} tokens nuevos encontrados"
INFO  "  🔍 Analizando {name} ({symbol}) en {chain}"
INFO  "  ✅ {name} — Score: {score}/100"
INFO  "  ❌ {name} — Descartado: {reason}"
INFO  "  📨 Alerta enviada: {name}"
INFO  "✅ Ciclo completado en {duration}s | {stats}"
INFO  "⏳ Próximo ciclo en {n}s"
```

### 9.5 Manejo de señales del sistema

Capturar `SIGINT` y `SIGTERM` para apagado limpio:
- Log "Sistema detenido por señal"
- Cerrar el pool de DB
- Salir con código 0

---

## 10. Archivo `requirements.txt`

```
asyncpg==0.29.0
aiohttp==3.9.5
python-dotenv==1.0.1
websockets==12.0
```

No incluir más dependencias. Si Claude Code necesita parsear algo extra, usar la librería estándar de Python.

---

## 11. Archivo `README.md`

Debe contener exactamente estas secciones:

### Setup rápido
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

### Obtener credenciales
- **CoinGecko API key**: registro en coingecko.com/en/api → plan Demo (gratuito)
- **Telegram bot**: hablar con @BotFather → /newbot → copiar token
- **Telegram chat_id**: agregar el bot a un canal/grupo → usar @userinfobot

### Variables de entorno (tabla completa)

### Ajustar los filtros (tabla con todas las variables de config y sus defaults)

### Estructura del proyecto (árbol de archivos con descripción de cada uno)

---

## 12. Criterios de aceptación

El sistema está completo cuando:

### 12.1 Setup

- [ ] `pip install -r requirements.txt` instala sin errores
- [ ] `python main.py` con `.env` válido arranca sin errores
- [ ] `python main.py` con `.env` incompleto falla con mensaje claro indicando qué falta
- [ ] La base de datos se crea automáticamente al primer arranque

### 12.2 Ciclo de polling

- [ ] Cada 5 minutos (configurable) se ejecuta un ciclo
- [ ] El ciclo obtiene tokens de CoinGecko `/coins/list/new`
- [ ] Solo procesa tokens en chains soportadas
- [ ] No reprocesa tokens ya analizados (verifica en DB)
- [ ] Las llamadas a GoPlus y a los datos de mercado son paralelas cuando aplica

### 12.3 Seguridad

- [ ] Auto-reject funciona correctamente para honeypots
- [ ] Auto-reject funciona para sell_tax > 50%
- [ ] Score se calcula correctamente restando penalizaciones
- [ ] Un token con is_mintable=True tiene score reducido en 25 puntos
- [ ] Un token con todos los flags en verde tiene score = 100

### 12.4 Persistencia

- [ ] Todos los tokens vistos quedan en tabla `tokens`
- [ ] Todos los checks de seguridad quedan en `security_checks` (incluyendo los rechazados)
- [ ] Todas las alertas enviadas quedan en `alerts_sent`
- [ ] El sistema puede ser reiniciado sin reprocesar tokens ya analizados

### 12.5 Alertas

- [ ] Tokens que pasan todos los filtros generan mensaje en Telegram
- [ ] El formato del mensaje es legible y contiene toda la información relevante
- [ ] No se envía alerta duplicada para el mismo token
- [ ] Si Telegram no está configurado, el mensaje se imprime en stdout

### 12.6 Resiliencia

- [ ] Si GoPlus API falla (timeout, error), el token se marca como no pasado y el ciclo continúa
- [ ] Si CoinGecko retorna 429 (rate limit), el ciclo espera y continúa
- [ ] Si falla el envío de Telegram, se loggea el error y el ciclo continúa
- [ ] Si falla la DB en una operación individual, se loggea y se continúa con el siguiente token
- [ ] El loop principal nunca se detiene por errores en tokens individuales

---

## 13. Comportamiento esperado en producción

### Primer arranque (base de datos vacía)

```
00:00:00 [INFO] 🚀 Altcoin Sniper MVP iniciando...
00:00:00 [INFO] ✅ Base de datos inicializada
00:00:01 [INFO] 🔄 Ciclo #1 iniciando — 2026-04-26 00:00:01
00:00:02 [INFO]   📋 47 tokens nuevos encontrados
00:00:02 [INFO]   🔍 Analizando PepeCoin2 (PEPE2) en ethereum
00:00:03 [INFO]   ❌ PepeCoin2 — Descartado: Honeypot detectado
00:00:03 [INFO]   🔍 Analizando SafeMoon99 (SAFE99) en bsc
00:00:04 [INFO]   ❌ SafeMoon99 — Descartado: Sell tax crítico: 99%
00:00:04 [INFO]   🔍 Analizando ProjectX (PJX) en ethereum
00:00:05 [INFO]   ✅ ProjectX — Score: 75/100
00:00:06 [INFO]   📨 Alerta enviada: ProjectX
          ...
00:00:45 [INFO] ✅ Ciclo completado en 44s | found=47 checked=47 passed=3 alerts=3 errors=0
00:00:45 [INFO] ⏳ Próximo ciclo en 300s
```

### Ciclos posteriores (tokens ya en DB)

```
00:05:45 [INFO] 🔄 Ciclo #2 iniciando — 2026-04-26 00:05:45
00:05:46 [INFO]   📋 12 tokens nuevos encontrados
00:05:46 [INFO]   📋 35 ya procesados anteriormente — omitidos
00:05:46 [INFO]   🔍 Analizando NuevoToken (NTK) en base
          ...
```

---

## 14. Notas para Claude Code

### Orden de implementación recomendado

1. `config.py` — primero, lo usan todos los demás
2. `database.py` — segundo, verificar que la conexión funciona
3. `security.py` — tercero, es el módulo más crítico
4. `fetcher.py` — cuarto
5. `alerts.py` — quinto
6. `main.py` — último, orquesta todo
7. `.env.example` y `README.md` — al final

### Convenciones de código

- Todas las funciones que hacen I/O deben ser `async`
- Usar type hints en todas las funciones públicas
- Docstring obligatorio en todas las funciones públicas
- Manejo de excepciones con `except Exception as e` en el nivel de orquestación
- En los módulos internos, dejar que las excepciones suban (el orquestador las captura)

### Testing básico sin ejecutar el sistema completo

Para verificar cada módulo de forma aislada, Claude Code puede crear un archivo `test_module.py` temporal que haga:

```python
import asyncio
from security import check_token

# Token de prueba conocido como scam (BSC)
result = asyncio.run(check_token(
    address="0x64c37c3d6b5ff0fdea26eec0c8b6de487105291c",
    chain_id="56"
))
assert result["passed_filters"] == False
print("✅ security.py funciona correctamente")
```

### Lo que NO debe hacer Claude Code

- No crear tests unitarios formales (pytest) para el MVP
- No crear Docker/docker-compose para el MVP
- No crear CI/CD
- No crear una API REST o interfaz web
- No agregar dependencias no listadas en requirements.txt
- No implementar el WebSocket (eso es Fase 2)
- No implementar modelos ML (eso es Fase 3)