"""Loop principal de polling y orquestación."""

import asyncio
import logging
import signal
import sys
import time
from datetime import datetime

import alerts
import database
import fetcher
import security
from config import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("sniper")

CHAIN_DISPLAY = {
    "1": "ethereum",
    "56": "bsc",
    "137": "polygon",
    "8453": "base",
    "42161": "arbitrum",
    "43114": "avalanche",
    "10": "optimism",
}


async def _process_token(token: dict, semaphore: asyncio.Semaphore, stats: dict) -> None:
    """Procesa un token: save → security → market → alert."""
    async with semaphore:
        address = token["address"]
        chain_id = token["chain_id"]
        name = token.get("name") or "?"
        symbol = token.get("symbol") or "?"
        chain_label = CHAIN_DISPLAY.get(chain_id, chain_id)

        logger.info("  🔍 Analizando %s (%s) en %s", name, symbol, chain_label)

        try:
            await database.save_token(
                address=address,
                chain_id=chain_id,
                name=name,
                symbol=symbol,
                coingecko_id=token.get("coingecko_id"),
            )
        except Exception as e:
            logger.error("Error guardando token %s: %s", address, e)
            stats["errors"] += 1
            return

        try:
            sec_result = await security.check_token(address, chain_id)
        except Exception as e:
            logger.error("Error inesperado en check_token %s: %s", address, e)
            stats["errors"] += 1
            return

        market_data: dict | None = None
        coingecko_id = token.get("coingecko_id")

        if coingecko_id:
            try:
                market_data = await fetcher.get_market_data(coingecko_id)
                if market_data:
                    sec_result["liquidity_usd"] = market_data.get("liquidity_usd", 0.0)
                    sec_result["market_cap_usd"] = market_data.get("market_cap_usd", 0.0)
                    sec_result["fdv_usd"] = market_data.get("fdv_usd", 0.0)
                    sec_result["volume_24h_usd"] = market_data.get("volume_24h_usd", 0.0)
            except Exception as e:
                logger.error("Error obteniendo market data %s: %s", coingecko_id, e)

        try:
            await database.save_security_check(sec_result)
        except Exception as e:
            logger.error("Error guardando security_check %s: %s", address, e)
            stats["errors"] += 1

        score = float(sec_result.get("security_score") or 0)
        is_scam = not sec_result.get("passed_filters")

        if is_scam:
            reason = sec_result.get("reject_reason") or "Sin razón específica"
            logger.info("  ⚠️ %s — Score %.0f/100 (SCAM: %s)", name, score, reason)
        else:
            logger.info("  ✅ %s — Score: %.0f/100", name, score)
            stats["passed_security"] += 1

        try:
            already = await database.was_alerted(address, chain_id)
        except Exception as e:
            logger.error("Error verificando was_alerted %s: %s", address, e)
            already = False

        if already:
            logger.info("  ℹ️  %s — Alerta ya enviada anteriormente, omitiendo", name)
            return

        try:
            sent = await alerts.send_token_alert(token, sec_result, market_data)
        except Exception as e:
            logger.error("Error enviando alerta %s: %s", name, e)
            stats["errors"] += 1
            return

        if sent:
            logger.info("  📨 Alerta enviada: %s", name)
            stats["alerts_sent"] += 1
            try:
                await database.save_alert(
                    address=address,
                    chain_id=chain_id,
                    score=score,
                    name=name,
                    symbol=symbol,
                )
            except Exception as e:
                logger.error("Error guardando alert record %s: %s", name, e)


async def run_cycle() -> dict:
    """Ejecuta un ciclo completo de polling."""
    stats = {
        "tokens_found": 0,
        "already_checked": 0,
        "passed_security": 0,
        "alerts_sent": 0,
        "errors": 0,
        "duration_seconds": 0.0,
    }
    started = time.monotonic()

    try:
        results = await asyncio.gather(
            fetcher.get_recently_added(),
            fetcher.gt_get_recently_added(),
            return_exceptions=True,
        )
    except Exception as e:
        logger.error("Error obteniendo tokens nuevos: %s", e)
        stats["errors"] += 1
        stats["duration_seconds"] = round(time.monotonic() - started, 2)
        return stats

    tokens_cg: list[dict] = results[0] if isinstance(results[0], list) else []
    tokens_gt: list[dict] = results[1] if isinstance(results[1], list) else []

    if isinstance(results[0], Exception):
        logger.error("Error en fuente CoinGecko: %s", results[0])
        stats["errors"] += 1
    if isinstance(results[1], Exception):
        logger.error("Error en fuente GeckoTerminal: %s", results[1])
        stats["errors"] += 1

    # Fusionar y deduplicar por (address, chain_id); CoinGecko tiene prioridad
    seen_keys: set[tuple[str, str]] = set()
    tokens: list[dict] = []
    for t in tokens_cg + tokens_gt:
        key = (t["address"], t["chain_id"])
        if key not in seen_keys:
            seen_keys.add(key)
            tokens.append(t)

    stats["tokens_found"] = len(tokens)
    logger.info(
        "  📋 CoinGecko: %d | GeckoTerminal: %d | Total único: %d",
        len(tokens_cg), len(tokens_gt), len(tokens),
    )

    pending: list[dict] = []
    for token in tokens:
        try:
            checked = await database.was_checked(token["address"], token["chain_id"])
        except Exception as e:
            logger.error("Error verificando was_checked: %s", e)
            stats["errors"] += 1
            continue
        if checked:
            stats["already_checked"] += 1
        else:
            pending.append(token)

    if stats["already_checked"]:
        logger.info("  📋 %d ya procesados anteriormente — omitidos", stats["already_checked"])

    if pending:
        semaphore = asyncio.Semaphore(5)
        tasks = [_process_token(t, semaphore, stats) for t in pending]
        await asyncio.gather(*tasks, return_exceptions=False)

    stats["duration_seconds"] = round(time.monotonic() - started, 2)
    return stats


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    def _handler(signum, frame):
        logger.info("🛑 Sistema detenido por señal %s", signum)
        stop_event.set()

    try:
        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)
    except (AttributeError, ValueError):
        # Windows no soporta SIGTERM en algunos contextos
        pass


async def main() -> int:
    logger.info("🚀 Altcoin Sniper MVP iniciando...")

    try:
        await database.setup_db()
    except Exception as e:
        logger.error("❌ No se pudo inicializar la base de datos: %s", e)
        return 1
    logger.info("✅ Base de datos inicializada")

    try:
        await alerts.send_system_message(
            f"🚀 *Altcoin Sniper MVP* iniciado\n"
            f"Polling cada {config.POLL_INTERVAL_SECONDS}s | "
            f"min score {config.MIN_SECURITY_SCORE:.0f} | "
            f"min liquidez ${config.MIN_LIQUIDITY_USD:,.0f}"
        )
    except Exception as e:
        logger.warning("No se pudo enviar mensaje de inicio a Telegram: %s", e)

    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)

    cycle = 0
    try:
        while not stop_event.is_set():
            cycle += 1
            logger.info("🔄 Ciclo #%d iniciando — %s", cycle, datetime.now().isoformat(timespec="seconds"))
            try:
                stats = await run_cycle()
            except Exception as e:
                logger.exception("Error fatal en ciclo #%d (continuando): %s", cycle, e)
                stats = {"duration_seconds": 0.0}

            duration = stats.get("duration_seconds", 0.0)
            summary = " ".join(
                f"{k}={v}" for k, v in stats.items() if k != "duration_seconds"
            )
            logger.info("✅ Ciclo completado en %ss | %s", duration, summary)

            if cycle % 10 == 0:
                try:
                    db_stats = await database.get_stats()
                    logger.info("📈 Acumulado: %s", db_stats)
                except Exception as e:
                    logger.warning("No se pudieron obtener stats acumuladas: %s", e)

            logger.info("⏳ Próximo ciclo en %ds", config.POLL_INTERVAL_SECONDS)

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=config.POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass
    finally:
        logger.info("Cerrando conexiones...")
        try:
            await database.close_db()
        except Exception as e:
            logger.warning("Error cerrando DB: %s", e)

    return 0


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
    except KeyboardInterrupt:
        exit_code = 0
    sys.exit(exit_code)
