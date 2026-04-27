"""Configuración central del sistema. Único punto de verdad para variables de entorno."""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"La variable {name} debe ser un entero, recibido: {raw!r}")


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"La variable {name} debe ser un número, recibido: {raw!r}")


@dataclass(frozen=True)
class Config:
    COINGECKO_API_KEY: str
    DATABASE_URL: str
    TELEGRAM_TOKEN: str
    TELEGRAM_CHAT_ID: str
    GOPLUS_API_KEY: str

    POLL_INTERVAL_SECONDS: int = 300
    MIN_SECURITY_SCORE: float = 60.0
    MIN_LIQUIDITY_USD: float = 50_000.0
    MAX_SELL_TAX: float = 0.10
    MAX_BUY_TAX: float = 0.10

    SUPPORTED_CHAINS: dict = field(default_factory=lambda: {
        "ethereum":              "1",
        "binance-smart-chain":   "56",
        "polygon-pos":           "137",
        "base":                  "8453",
        "arbitrum-one":          "42161",
        "avalanche":             "43114",
        "optimistic-ethereum":   "10",
    })

    # Mapeo de slugs de red de GeckoTerminal → chain_id interno
    GT_NETWORKS: dict = field(default_factory=lambda: {
        "eth":          "1",
        "bsc":          "56",
        "polygon_pos":  "137",
        "base":         "8453",
        "arbitrum":     "42161",
        "avax":         "43114",
        "optimism":     "10",
    })


def _load_config() -> Config:
    required = {
        "DATABASE_URL": os.getenv("DATABASE_URL", "").strip(),
        "TELEGRAM_TOKEN": os.getenv("TELEGRAM_TOKEN", "").strip(),
        "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID", "").strip(),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError(
            "Faltan variables de entorno obligatorias: "
            + ", ".join(missing)
            + ". Revisa tu archivo .env (usa .env.example como plantilla)."
        )

    return Config(
        COINGECKO_API_KEY=os.getenv("COINGECKO_API_KEY", "").strip(),
        DATABASE_URL=required["DATABASE_URL"],
        TELEGRAM_TOKEN=required["TELEGRAM_TOKEN"],
        TELEGRAM_CHAT_ID=required["TELEGRAM_CHAT_ID"],
        GOPLUS_API_KEY=os.getenv("GOPLUS_API_KEY", "").strip(),
        POLL_INTERVAL_SECONDS=_get_int("POLL_INTERVAL_SECONDS", 300),
        MIN_SECURITY_SCORE=_get_float("MIN_SECURITY_SCORE", 60.0),
        MIN_LIQUIDITY_USD=_get_float("MIN_LIQUIDITY_USD", 50_000.0),
        MAX_SELL_TAX=_get_float("MAX_SELL_TAX", 0.10),
        MAX_BUY_TAX=_get_float("MAX_BUY_TAX", 0.10),
    )


config = _load_config()
