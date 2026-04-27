"""Formateador y enviador de alertas a Telegram."""

import asyncio
import logging

import aiohttp

from config import config

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
REQUEST_TIMEOUT = 10


CHAIN_NAMES = {
    "1": "ethereum",
    "56": "binance-smart-chain",
    "137": "polygon",
    "8453": "base",
    "42161": "arbitrum",
    "43114": "avalanche",
    "10": "optimism",
}


def _flag(value: bool | None, good_when_false: bool = True) -> str:
    if value is None:
        return "❓ N/A"
    if good_when_false:
        return "✅ No" if value is False else "❌ Sí"
    return "✅ Sí" if value is True else "⚠️ No"


def _fmt_money(value: float | None) -> str:
    if value is None or value == 0:
        return "N/A"
    if value >= 1:
        return f"${value:,.2f}"
    return f"${value:.6f}"


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value * 100:.1f}%"


def _score_emoji(score: float) -> str:
    if score >= 80:
        return "🟢"
    if score >= 65:
        return "🟡"
    if score >= 30:
        return "🟠"
    return "🔴"


def _format_alert(token: dict, security: dict, market: dict | None) -> str:
    score = float(security.get("security_score") or 0)
    emoji = _score_emoji(score)
    is_scam = not security.get("passed_filters", True)

    name = token.get("name") or "Desconocido"
    symbol = token.get("symbol") or "?"
    address = token.get("address") or security.get("address") or ""
    chain_id = token.get("chain_id") or security.get("chain_id") or ""
    chain_name = CHAIN_NAMES.get(chain_id, f"chain_{chain_id}")

    lines: list[str] = []
    if is_scam:
        lines.append("🔴 *⚠️ SCAM DETECTADO — Token Riesgoso*")
    else:
        lines.append(f"{emoji} *Nuevo Token Detectado*")
    lines.append("")
    lines.append(f"*{name}* (`{symbol}`)")
    lines.append(f"Chain: `{chain_name}`")
    source = token.get("source") or "coingecko"
    source_label = "GeckoTerminal" if source == "geckoterminal" else "CoinGecko"
    lines.append(f"Fuente: `{source_label}`")
    lines.append(f"Contrato: `{address}`")
    lines.append("")
    lines.append(f"*Score de Seguridad: {score:.0f}/100*")
    lines.append("")

    lines.append("📊 *Mercado*")
    if market:
        price = market.get("price_usd") or 0
        mc = market.get("market_cap_usd") or 0
        fdv = market.get("fdv_usd") or 0
        vol = market.get("volume_24h_usd") or 0
        ratio = (fdv / mc) if mc > 0 else 0
        lines.append(f"• Precio: {_fmt_money(price)}")
        lines.append(f"• Market Cap: {_fmt_money(mc)}")
        lines.append(f"• FDV: {_fmt_money(fdv)}")
        lines.append(f"• FDV/MC: {ratio:.1f}x" if ratio else "• FDV/MC: N/A")
        lines.append(f"• Volumen 24h: {_fmt_money(vol)}")
    else:
        lines.append("• Datos de mercado no disponibles")
    lines.append("")

    lines.append("🔐 *Seguridad*")
    lines.append(f"• Honeypot: {_flag(security.get('is_honeypot'))}")
    lines.append(f"• Sell Tax: {_fmt_pct(security.get('sell_tax'))}")
    lines.append(f"• Buy Tax: {_fmt_pct(security.get('buy_tax'))}")
    lines.append(f"• Mintable: {_flag(security.get('is_mintable'))}")
    lines.append(f"• Blacklist: {_flag(security.get('has_blacklist'))}")
    lines.append(f"• Open Source: {_flag(security.get('is_open_source'), good_when_false=False)}")
    lines.append("")

    lines.append("⚠️ *Alertas detectadas*")
    reject_reason = security.get("reject_reason")
    if reject_reason:
        for reason in reject_reason.split(";"):
            reason = reason.strip()
            if reason:
                lines.append(f"• {reason}")
    else:
        lines.append("• Ninguna")
    lines.append("")

    if market and market.get("links"):
        links = market["links"]
        link_parts: list[str] = []
        if links.get("homepage"):
            link_parts.append(f"[Web]({links['homepage']})")
        if links.get("twitter"):
            link_parts.append(f"[Twitter](https://twitter.com/{links['twitter']})")
        if links.get("telegram"):
            link_parts.append(f"[Telegram](https://t.me/{links['telegram']})")
        if links.get("github"):
            link_parts.append(f"[GitHub]({links['github']})")
        if link_parts:
            lines.append("🔗 " + " | ".join(link_parts))
            lines.append("")

    lines.append("_Análisis automatizado. No es consejo financiero._")
    return "\n".join(lines)


async def _send_to_telegram(text: str) -> bool:
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        print("[TELEGRAM SIMULADO]")
        print(text)
        return True

    url = TELEGRAM_API.format(token=config.TELEGRAM_TOKEN)
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    try:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error("Telegram HTTP %d: %s", resp.status, body[:300])
                    return False
                return True
    except asyncio.TimeoutError:
        logger.error("Timeout enviando mensaje a Telegram")
        return False
    except aiohttp.ClientError as e:
        logger.error("Error de red enviando a Telegram: %s", e)
        return False
    except Exception as e:
        logger.error("Error inesperado enviando a Telegram: %s", e)
        return False


async def send_token_alert(token: dict, security: dict, market: dict | None) -> bool:
    """Envía alerta de token nuevo que pasó los filtros."""
    text = _format_alert(token, security, market)
    return await _send_to_telegram(text)


async def send_system_message(text: str) -> bool:
    """Envía mensaje del sistema (errores, stats, inicio/parada)."""
    return await _send_to_telegram(text)
