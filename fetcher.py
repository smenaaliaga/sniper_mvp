"""Cliente CoinGecko REST API: tokens nuevos y datos de mercado."""

import asyncio
import logging
from typing import Any

import aiohttp

from config import config

logger = logging.getLogger(__name__)

COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"
REQUEST_TIMEOUT = 15
RATE_LIMIT_BACKOFF = 60

GECKOTERMINAL_BASE_URL = "https://api.geckoterminal.com/api/v2"
GT_REQUEST_TIMEOUT = 15
GT_RATE_LIMIT_BACKOFF = 60

# IDs ya vistos en ciclos anteriores. Vacío al arrancar → primer ciclo
# trata todo como "nuevo" (la DB filtra los ya procesados vía was_checked).
_known_ids: set[str] = set()
_first_run: bool = True

# Estado GeckoTerminal: mismo patrón de baseline que CoinGecko.
_gt_known_pool_ids: set[str] = set()
_gt_first_run: bool = True

# Índice (address, chain_id) → coingecko_id, construido desde la lista completa de CoinGecko.
# Permite resolver coingecko_id para tokens descubiertos vía GeckoTerminal.
_cg_address_index: dict[tuple[str, str], str] = {}


def _build_headers() -> dict:
    headers = {"accept": "application/json"}
    if config.COINGECKO_API_KEY:
        headers["x-cg-demo-api-key"] = config.COINGECKO_API_KEY
    return headers


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


async def get_recently_added() -> list[dict]:
    """
    Obtiene tokens recientemente añadidos a CoinGecko en chains soportadas.

    Usa /coins/list?include_platform=true (disponible en el tier gratuito/demo)
    y detecta tokens nuevos comparando contra el set en memoria del ciclo anterior.
    El primer ciclo descarga la lista completa y la marca como "conocida" sin
    retornar nada, evitando un aluvión de alertas al arrancar.
    """
    global _known_ids, _first_run

    url = f"{COINGECKO_BASE_URL}/coins/list"
    params = {"include_platform": "true"}
    headers = _build_headers()

    try:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status == 429:
                    logger.warning(
                        "⚠️  CoinGecko rate limit (429). Esperando %ds...", RATE_LIMIT_BACKOFF,
                    )
                    await asyncio.sleep(RATE_LIMIT_BACKOFF)
                    return []
                if resp.status != 200:
                    body = await resp.text()
                    logger.error("CoinGecko /coins/list HTTP %d: %s", resp.status, body[:200])
                    return []

                payload = await resp.json()
    except asyncio.TimeoutError:
        logger.error("Timeout consultando CoinGecko /coins/list")
        return []
    except aiohttp.ClientError as e:
        logger.error("Error de red en CoinGecko: %s", e)
        return []
    except Exception as e:
        logger.error("Error inesperado en CoinGecko: %s", e)
        return []

    if not isinstance(payload, list):
        logger.warning("CoinGecko /coins/list retornó formato inesperado")
        return []

    supported = config.SUPPORTED_CHAINS
    current_ids: set[str] = set()
    all_tokens: list[dict] = []

    for entry in payload:
        if not isinstance(entry, dict):
            continue

        coingecko_id = entry.get("id")
        if not coingecko_id:
            continue

        current_ids.add(coingecko_id)

        name = entry.get("name") or ""
        symbol = (entry.get("symbol") or "").upper()
        platforms = entry.get("platforms") or {}

        if not isinstance(platforms, dict):
            continue

        for platform_name, address in platforms.items():
            if platform_name not in supported:
                continue
            if not address or len(address) <= 10:
                continue

            addr = address.lower()
            cid = supported[platform_name]
            all_tokens.append({
                "coingecko_id": coingecko_id,
                "name": name,
                "symbol": symbol,
                "address": addr,
                "chain_id": cid,
                "platform": platform_name,
                "source": "coingecko",
            })
            _cg_address_index[(addr, cid)] = coingecko_id

    if _first_run:
        # Primer ciclo: memorizar el estado actual como "ya conocido"
        _known_ids = current_ids
        _first_run = False
        total = len([t for t in all_tokens])
        logger.info(
            "  📋 Primer ciclo: %d tokens en CoinGecko memorizados como base. "
            "Los nuevos se detectarán en el próximo ciclo.",
            len(current_ids),
        )
        return []

    new_ids = current_ids - _known_ids
    _known_ids = current_ids

    if not new_ids:
        return []

    return [t for t in all_tokens if t["coingecko_id"] in new_ids]


async def gt_get_recently_added() -> list[dict]:
    """
    Obtiene pools nuevos de GeckoTerminal en chains soportadas.

    Usa GET /networks/new_pools (API pública, gratuita, ~10 req/min).
    El token de interés es el base_token de cada pool nuevo.
    Primer ciclo: memoriza pools actuales sin retornar nada.
    Ciclos siguientes: retorna solo los pools nuevos como tokens normalizados.
    """
    global _gt_known_pool_ids, _gt_first_run

    url = f"{GECKOTERMINAL_BASE_URL}/networks/new_pools"
    params = {"include": "base_token,quote_token", "page": "1"}
    headers = {"accept": "application/json;version=20230203"}

    try:
        timeout = aiohttp.ClientTimeout(total=GT_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status == 429:
                    logger.warning(
                        "⚠️  GeckoTerminal rate limit (429). Esperando %ds...", GT_RATE_LIMIT_BACKOFF,
                    )
                    await asyncio.sleep(GT_RATE_LIMIT_BACKOFF)
                    return []
                if resp.status != 200:
                    body = await resp.text()
                    logger.error("GeckoTerminal /networks/new_pools HTTP %d: %s", resp.status, body[:200])
                    return []
                payload = await resp.json()
    except asyncio.TimeoutError:
        logger.error("Timeout consultando GeckoTerminal /networks/new_pools")
        return []
    except aiohttp.ClientError as e:
        logger.error("Error de red en GeckoTerminal: %s", e)
        return []
    except Exception as e:
        logger.error("Error inesperado en GeckoTerminal: %s", e)
        return []

    pools = payload.get("data") or []
    included = payload.get("included") or []

    if not isinstance(pools, list):
        logger.warning("GeckoTerminal /networks/new_pools retornó formato inesperado")
        return []

    # Construir lookup de tokens incluidos: gt_id → attributes
    token_attrs: dict[str, dict] = {}
    for item in included:
        if isinstance(item, dict) and item.get("type") == "token":
            token_attrs[item["id"]] = item.get("attributes") or {}

    supported_gt = config.GT_NETWORKS
    current_pool_ids: set[str] = set()
    new_tokens: list[dict] = []

    for pool in pools:
        if not isinstance(pool, dict):
            continue
        pool_id = pool.get("id")
        if not pool_id:
            continue
        current_pool_ids.add(pool_id)

        if _gt_first_run or pool_id in _gt_known_pool_ids:
            continue

        rels = pool.get("relationships") or {}
        network_slug = ((rels.get("network") or {}).get("data") or {}).get("id") or ""
        if network_slug not in supported_gt:
            continue

        chain_id = supported_gt[network_slug]

        base_token_gt_id = ((rels.get("base_token") or {}).get("data") or {}).get("id") or ""
        if not base_token_gt_id:
            continue

        attrs = token_attrs.get(base_token_gt_id) or {}
        address = (attrs.get("address") or "").lower()
        name = attrs.get("name") or ""
        symbol = (attrs.get("symbol") or "").upper()

        if not address or len(address) <= 10:
            continue

        # Intentar resolver coingecko_id desde el índice construido por CoinGecko
        coingecko_id = _cg_address_index.get((address, chain_id))

        new_tokens.append({
            "coingecko_id": coingecko_id,
            "name": name,
            "symbol": symbol,
            "address": address,
            "chain_id": chain_id,
            "platform": network_slug,
            "source": "geckoterminal",
            "source_ref": pool_id,
        })

    if _gt_first_run:
        _gt_known_pool_ids = current_pool_ids
        _gt_first_run = False
        logger.info(
            "  📋 GeckoTerminal: %d pools memorizados como base. "
            "Los nuevos se detectarán en el próximo ciclo.",
            len(current_pool_ids),
        )
        return []

    _gt_known_pool_ids = current_pool_ids
    return new_tokens


async def get_market_data(coingecko_id: str) -> dict | None:
    """Obtiene datos de mercado para un token específico. None si error."""
    url = f"{COINGECKO_BASE_URL}/coins/{coingecko_id}"
    params = {
        "localization": "false",
        "tickers": "false",
        "market_data": "true",
        "community_data": "false",
        "developer_data": "true",
        "sparkline": "false",
    }
    headers = _build_headers()

    try:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status == 429:
                    logger.warning("⚠️  CoinGecko rate limit (429) en /coins/%s", coingecko_id)
                    await asyncio.sleep(RATE_LIMIT_BACKOFF)
                    return None
                if resp.status == 404:
                    return None
                if resp.status != 200:
                    body = await resp.text()
                    logger.error("CoinGecko /coins/%s HTTP %d: %s", coingecko_id, resp.status, body[:200])
                    return None

                payload = await resp.json()
    except asyncio.TimeoutError:
        logger.error("Timeout consultando CoinGecko /coins/%s", coingecko_id)
        return None
    except aiohttp.ClientError as e:
        logger.error("Error de red consultando %s: %s", coingecko_id, e)
        return None
    except Exception as e:
        logger.error("Error inesperado consultando %s: %s", coingecko_id, e)
        return None

    market_data = payload.get("market_data") or {}
    dev_data = payload.get("developer_data") or {}
    links = payload.get("links") or {}

    price_usd       = _safe_float((market_data.get("current_price") or {}).get("usd"))
    market_cap_usd  = _safe_float((market_data.get("market_cap") or {}).get("usd"))
    fdv_usd         = _safe_float(
        (market_data.get("fully_diluted_valuation") or {}).get("usd")
    )
    volume_24h_usd  = _safe_float((market_data.get("total_volume") or {}).get("usd"))
    price_change_24h = _safe_float(market_data.get("price_change_percentage_24h"))

    # liquidez: aproximación = 10% del market cap si no hay un dato directo
    liquidity_usd = market_cap_usd * 0.1 if market_cap_usd > 0 else 0.0

    homepage_list = links.get("homepage") or []
    homepage = next((h for h in homepage_list if h), None)

    twitter_username = links.get("twitter_screen_name") or None
    telegram_channel = links.get("telegram_channel_identifier") or None

    repos = (links.get("repos_url") or {}).get("github") or []
    github_url = next((r for r in repos if r), None)

    return {
        "price_usd": price_usd,
        "market_cap_usd": market_cap_usd,
        "fdv_usd": fdv_usd,
        "volume_24h_usd": volume_24h_usd,
        "liquidity_usd": liquidity_usd,
        "price_change_24h": price_change_24h,
        "github_stars": _safe_int(dev_data.get("stars")),
        "github_commits_4w": _safe_int(dev_data.get("commit_count_4_weeks")),
        "links": {
            "homepage": homepage,
            "twitter": twitter_username,
            "telegram": telegram_channel,
            "github": github_url,
        },
    }
