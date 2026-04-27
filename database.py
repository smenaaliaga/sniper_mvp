"""Schema PostgreSQL y operaciones CRUD async."""

import logging
from typing import Optional

import asyncpg

from config import config

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None


SCHEMA_SQL = """
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

    liquidity_usd               NUMERIC(20,2),
    market_cap_usd              NUMERIC(20,2),
    fdv_usd                     NUMERIC(20,2),
    volume_24h_usd              NUMERIC(20,2),

    security_score              NUMERIC(5,2) NOT NULL,
    passed_filters              BOOLEAN NOT NULL,
    reject_reason               TEXT,

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

CREATE INDEX IF NOT EXISTS idx_tokens_address_chain
    ON tokens(address, chain_id);

CREATE INDEX IF NOT EXISTS idx_security_address_chain
    ON security_checks(token_address, chain_id);

CREATE INDEX IF NOT EXISTS idx_alerts_address_chain
    ON alerts_sent(token_address, chain_id);
"""


SECURITY_INSERT_COLUMNS = [
    "token_address", "chain_id",
    "is_honeypot", "sell_tax", "buy_tax",
    "hidden_owner", "can_take_back_ownership", "is_mintable",
    "has_blacklist", "transfer_pausable", "is_open_source",
    "is_proxy", "slippage_modifiable", "owner_address",
    "liquidity_usd", "market_cap_usd", "fdv_usd", "volume_24h_usd",
    "security_score", "passed_filters", "reject_reason",
    "api_error",
]


async def setup_db() -> None:
    """Inicializa el connection pool y crea el schema si no existe."""
    global _pool
    if _pool is not None:
        return

    _pool = await asyncpg.create_pool(
        dsn=config.DATABASE_URL,
        min_size=2,
        max_size=10,
    )
    async with _pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)


async def close_db() -> None:
    """Cierra el connection pool. Llamar al apagar el sistema."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def _get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("La DB no está inicializada. Llama a setup_db() primero.")
    return _pool


async def save_token(
    address: str,
    chain_id: str,
    name: str,
    symbol: str,
    coingecko_id: str | None = None,
) -> None:
    """Inserta un token nuevo. Si ya existe (address + chain_id), no hace nada."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO tokens (address, chain_id, name, symbol, coingecko_id)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (address, chain_id) DO NOTHING
            """,
            address, chain_id, name, symbol, coingecko_id,
        )


async def save_security_check(data: dict) -> None:
    """Guarda el resultado completo del análisis de seguridad."""
    pool = _get_pool()

    address = data.get("address") or data.get("token_address")
    row = {
        "token_address":           address,
        "chain_id":                data.get("chain_id"),
        "is_honeypot":             data.get("is_honeypot"),
        "sell_tax":                data.get("sell_tax"),
        "buy_tax":                 data.get("buy_tax"),
        "hidden_owner":            data.get("hidden_owner"),
        "can_take_back_ownership": data.get("can_take_back_ownership"),
        "is_mintable":             data.get("is_mintable"),
        "has_blacklist":           data.get("has_blacklist"),
        "transfer_pausable":       data.get("transfer_pausable"),
        "is_open_source":          data.get("is_open_source"),
        "is_proxy":                data.get("is_proxy"),
        "slippage_modifiable":     data.get("slippage_modifiable"),
        "owner_address":           data.get("owner_address"),
        "liquidity_usd":           data.get("liquidity_usd"),
        "market_cap_usd":          data.get("market_cap_usd"),
        "fdv_usd":                 data.get("fdv_usd"),
        "volume_24h_usd":          data.get("volume_24h_usd"),
        "security_score":          data.get("security_score", 0),
        "passed_filters":          data.get("passed_filters", False),
        "reject_reason":           data.get("reject_reason"),
        "api_error":               data.get("api_error"),
    }

    placeholders = ", ".join(f"${i+1}" for i in range(len(SECURITY_INSERT_COLUMNS)))
    columns = ", ".join(SECURITY_INSERT_COLUMNS)
    values = [row[c] for c in SECURITY_INSERT_COLUMNS]

    async with pool.acquire() as conn:
        await conn.execute(
            f"INSERT INTO security_checks ({columns}) VALUES ({placeholders})",
            *values,
        )


async def was_checked(address: str, chain_id: str) -> bool:
    """Retorna True si el token ya tiene un registro en security_checks."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchval(
            """
            SELECT 1 FROM security_checks
            WHERE token_address = $1 AND chain_id = $2
            LIMIT 1
            """,
            address, chain_id,
        )
        return row is not None


async def was_alerted(address: str, chain_id: str) -> bool:
    """Retorna True si ya se envió una alerta para este token."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchval(
            """
            SELECT 1 FROM alerts_sent
            WHERE token_address = $1 AND chain_id = $2
            LIMIT 1
            """,
            address, chain_id,
        )
        return row is not None


async def save_alert(
    address: str,
    chain_id: str,
    score: float,
    name: str,
    symbol: str,
) -> None:
    """Registra que se envió una alerta para este token."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO alerts_sent (token_address, chain_id, security_score, token_name, token_symbol)
            VALUES ($1, $2, $3, $4, $5)
            """,
            address, chain_id, score, name, symbol,
        )


async def get_stats() -> dict:
    """Retorna estadísticas acumuladas del sistema."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        total_tokens_seen = await conn.fetchval("SELECT COUNT(*) FROM tokens")
        total_checked = await conn.fetchval(
            "SELECT COUNT(DISTINCT (token_address, chain_id)) FROM security_checks"
        )
        total_passed = await conn.fetchval(
            "SELECT COUNT(*) FROM security_checks WHERE passed_filters = TRUE"
        )
        total_alerts_sent = await conn.fetchval("SELECT COUNT(*) FROM alerts_sent")
        scams_detected = await conn.fetchval(
            "SELECT COUNT(*) FROM security_checks WHERE passed_filters = FALSE AND api_error IS NULL"
        )

    return {
        "total_tokens_seen": int(total_tokens_seen or 0),
        "total_checked": int(total_checked or 0),
        "total_passed": int(total_passed or 0),
        "total_alerts_sent": int(total_alerts_sent or 0),
        "scams_detected": int(scams_detected or 0),
    }
