"""Cliente GoPlus Security API + cálculo de score de seguridad."""

import asyncio
import logging
from typing import Any

import aiohttp

from config import config

logger = logging.getLogger(__name__)

GOPLUS_BASE_URL = "https://api.gopluslabs.io/api/v1/token_security"
REQUEST_TIMEOUT = 10


PENALTIES: list[tuple[str, int, str]] = [
    # (clave_dict, puntos_a_restar, descripción)
    ("can_take_back_ownership", 25, "Owner puede recuperar propiedad (-25)"),
    ("is_mintable",             25, "Token mintable (-25)"),
    ("has_blacklist",           20, "Tiene blacklist (-20)"),
    ("slippage_modifiable",     20, "Slippage modificable (-20)"),
    ("transfer_pausable",       15, "Transferencias pausables (-15)"),
    ("is_proxy",                10, "Contrato proxy (-10)"),
]


def _empty_result(address: str, chain_id: str) -> dict:
    return {
        "address": address,
        "chain_id": chain_id,
        "is_honeypot": None,
        "sell_tax": None,
        "buy_tax": None,
        "hidden_owner": None,
        "can_take_back_ownership": None,
        "is_mintable": None,
        "has_blacklist": None,
        "transfer_pausable": None,
        "is_open_source": None,
        "is_proxy": None,
        "slippage_modifiable": None,
        "owner_address": None,
        "liquidity_usd": 0.0,
        "market_cap_usd": 0.0,
        "fdv_usd": 0.0,
        "volume_24h_usd": 0.0,
        "security_score": 0.0,
        "passed_filters": False,
        "reject_reason": None,
        "api_error": None,
    }


def _to_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip()
    if s == "1":
        return True
    if s == "0":
        return False
    return None


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _parse_goplus_payload(raw: dict, address: str, chain_id: str) -> dict:
    result = _empty_result(address, chain_id)

    token_data = raw.get(address.lower())
    if not token_data:
        result["api_error"] = "Token no encontrado en la respuesta de GoPlus"
        return result

    result["is_honeypot"]              = _to_bool(token_data.get("is_honeypot"))
    result["sell_tax"]                 = _to_float(token_data.get("sell_tax"))
    result["buy_tax"]                  = _to_float(token_data.get("buy_tax"))
    result["hidden_owner"]             = _to_bool(token_data.get("hidden_owner"))
    result["can_take_back_ownership"]  = _to_bool(token_data.get("can_take_back_ownership"))
    result["is_mintable"]              = _to_bool(token_data.get("is_mintable"))
    result["has_blacklist"]            = _to_bool(token_data.get("is_blacklisted")) \
                                         if token_data.get("is_blacklisted") is not None \
                                         else _to_bool(token_data.get("has_blacklist"))
    result["transfer_pausable"]        = _to_bool(token_data.get("transfer_pausable"))
    result["is_open_source"]           = _to_bool(token_data.get("is_open_source"))
    result["is_proxy"]                 = _to_bool(token_data.get("is_proxy"))
    result["slippage_modifiable"]      = _to_bool(token_data.get("slippage_modifiable"))
    result["owner_address"]            = token_data.get("owner_address") or None

    return result


def _compute_score(result: dict) -> dict:
    # Pasada 1: auto-reject
    if result.get("api_error"):
        result["security_score"] = 0.0
        result["passed_filters"] = False
        result["reject_reason"] = f"Error de API: {result['api_error']}"
        return result

    if result.get("is_honeypot") is True:
        result["security_score"] = 0.0
        result["passed_filters"] = False
        result["reject_reason"] = "Honeypot detectado"
        return result

    sell_tax = result.get("sell_tax")
    if sell_tax is not None and sell_tax > 0.50:
        result["security_score"] = 0.0
        result["passed_filters"] = False
        result["reject_reason"] = f"Sell tax crítico: {sell_tax * 100:.1f}%"
        return result

    if result.get("hidden_owner") is True:
        result["security_score"] = 0.0
        result["passed_filters"] = False
        result["reject_reason"] = "Owner oculto"
        return result

    # Pasada 2: penalizaciones desde 100
    score = 100.0
    reasons: list[str] = []

    if sell_tax is not None and sell_tax > config.MAX_SELL_TAX:
        score -= 30
        reasons.append(f"Sell tax {sell_tax * 100:.1f}% > {config.MAX_SELL_TAX * 100:.0f}% (-30)")

    buy_tax = result.get("buy_tax")
    if buy_tax is not None and buy_tax > config.MAX_BUY_TAX:
        score -= 20
        reasons.append(f"Buy tax {buy_tax * 100:.1f}% > {config.MAX_BUY_TAX * 100:.0f}% (-20)")

    for key, points, description in PENALTIES:
        if result.get(key) is True:
            score -= points
            reasons.append(description)

    if result.get("is_open_source") is False:
        score -= 15
        reasons.append("Contrato no open source (-15)")

    score = max(0.0, min(100.0, score))
    result["security_score"] = round(score, 2)
    result["passed_filters"] = score >= config.MIN_SECURITY_SCORE
    result["reject_reason"] = "; ".join(reasons) if reasons else None
    return result


async def check_token(address: str, chain_id: str) -> dict:
    """Consulta GoPlus y retorna un dict con el resultado completo. Nunca lanza."""
    result = _empty_result(address, chain_id)

    url = f"{GOPLUS_BASE_URL}/{chain_id}"
    params = {"contract_addresses": address}
    headers = {}
    if config.GOPLUS_API_KEY:
        headers["Authorization"] = f"Bearer {config.GOPLUS_API_KEY}"

    try:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    result["api_error"] = f"HTTP {resp.status}: {body[:200]}"
                    return _compute_score(result)

                payload = await resp.json()
    except asyncio.TimeoutError:
        result["api_error"] = "Timeout consultando GoPlus"
        return _compute_score(result)
    except aiohttp.ClientError as e:
        result["api_error"] = f"Error de red: {e}"
        return _compute_score(result)
    except Exception as e:
        result["api_error"] = f"Error inesperado: {e}"
        return _compute_score(result)

    raw_result = payload.get("result")
    if not isinstance(raw_result, dict) or not raw_result:
        message = payload.get("message") or "Respuesta sin datos"
        result["api_error"] = f"GoPlus: {message}"
        return _compute_score(result)

    result = _parse_goplus_payload(raw_result, address, chain_id)
    return _compute_score(result)
