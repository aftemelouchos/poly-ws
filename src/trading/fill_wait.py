"""Fill onayi — oncelik User WS, kisa bakiye poll yedek."""

from __future__ import annotations

import asyncio
import logging
import time

from src.trading.gateway import TradingGateway

logger = logging.getLogger(__name__)

FAST_POLL = 0.06


async def wait_for_token_balance(
    gateway: TradingGateway,
    token_id: str,
    min_size: float,
    *,
    timeout: float = 4.0,
    poll_interval: float = FAST_POLL,
    min_ratio: float = 0.9,
    quiet: bool = True,
) -> tuple[bool, float]:
    deadline = time.monotonic() + timeout
    last = 0.0
    while time.monotonic() < deadline:
        bal = await gateway.get_balances([token_id])
        tb = bal.tokens.get(token_id)
        last = tb.balance if tb else 0.0
        if last >= min_size * min_ratio:
            if not quiet:
                logger.debug("Fill OK | %s... %.4f", token_id[:12], last)
            return True, last
        await asyncio.sleep(poll_interval)
    if not quiet:
        logger.debug(
            "Fill timeout | %s... got=%.4f (%.1fs)",
            token_id[:12],
            last,
            timeout,
        )
    return False, last


async def wait_for_token_cleared(
    gateway: TradingGateway,
    token_id: str,
    *,
    timeout: float = 4.0,
    poll_interval: float = FAST_POLL,
    max_remaining: float = 0.05,
    quiet: bool = True,
) -> tuple[bool, float]:
    deadline = time.monotonic() + timeout
    last = 0.0
    while time.monotonic() < deadline:
        bal = await gateway.get_balances([token_id])
        tb = bal.tokens.get(token_id)
        last = tb.balance if tb else 0.0
        if last <= max_remaining:
            if not quiet:
                logger.debug("Sell cleared | %s... %.4f", token_id[:12], last)
            return True, last
        await asyncio.sleep(poll_interval)
    if not quiet:
        logger.debug(
            "Sell clear timeout | %s... rem=%.4f (%.1fs)",
            token_id[:12],
            last,
            timeout,
        )
    return False, last
