"""Stock RSI v2 live engine — WS odakli, dusuk gecikme."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from src.live_strategy_v2.config import LiveStrategyV2Config
from src.live_strategy_v2.rsi_stream import BarSnapshot, RsiLiveStream
from src.market_resolver import MarketResolver
from src.models import BookState, Fill, MarketInfo, TokenLabel
from src.strategy_v2.config import StockRsiConfig, is_entry_price_safe
from src.strategy_v2.resolution import (
    entries_window_minutes,
    market_end_from_slug,
    minutes_to_expiry,
    resolution_phase,
    should_force_close,
)
from src.strategy_v2.types import Side
from src.trading.config import TradingConfig, load_trading_config
from src.trading.hft_runtime import HftTradingRuntime
from src.trading.pricing import round_down_shares
from src.trading.types import TradingMode

logger = logging.getLogger(__name__)

MIN_POSITION_SHARES = 0.5
# Orphan retry throttle — ayni token icin SELL spam'ini engelle (CLOB rate limit).
# Bu timeout DEGIL, sadece "iki retry arasi en az X saniye" anlamina gelir.
ORPHAN_RETRY_INTERVAL = 0.4


class StockRsiLiveEngine:
    """
    Canli Strategy v2:
    - Market WS -> UP mid -> RSI cross
    - cross up: UP al (DOWN varsa sat)
    - cross down: UP sat, DOWN al
    - Pozisyon: User WS trade event'leri ile takip (REST = sadece sync + periyodik)
    """

    def __init__(
        self,
        live_cfg: LiveStrategyV2Config,
        trading_cfg: TradingConfig | None = None,
    ):
        self.live_cfg = live_cfg
        self.trading_cfg = trading_cfg or load_trading_config()

        from dataclasses import replace

        strat = live_cfg.strategy
        if strat.size <= 0:
            strat = replace(strat, size=self.trading_cfg.default_order_size)
        self.strat_cfg = strat

        self.state = BookState(mode=self.trading_cfg.mode.value)
        self.rt = HftTradingRuntime(self.trading_cfg, self.state)
        self.rsi_stream = RsiLiveStream(self.strat_cfg)
        self.resolver = MarketResolver(live_cfg.gamma_base_url)

        self._position = Side.FLAT
        self._last_trade_at: datetime | None = None
        self._trade_lock = asyncio.Lock()
        self._running = False
        self._market: MarketInfo | None = None
        self._rollover_task: asyncio.Task | None = None
        self._balance_task: asyncio.Task | None = None
        self._resolution_phase: str = ""
        self._market_end_dt: datetime | None = None
        # Orphan reconcile: SELL fail olduysa hangi token agresif retry'a alindi
        self._needs_flatten: set[TokenLabel] = set()
        self._last_orphan_retry: dict[TokenLabel, float] = {}
        self.signals_up = 0
        self.signals_down = 0
        self.trades_executed = 0

    @property
    def market(self) -> MarketInfo | None:
        return self._market

    async def start(self) -> None:
        self._running = True
        market = await self._resolve_market()
        await self._attach_market_and_wire(market)
        self.rt.on_fill(self._on_fill)
        self.rt.on_trade(self._on_user_trade)

        if self.live_cfg.market.auto_rollover:
            self._rollover_task = asyncio.create_task(self._rollover_loop())

        if self.live_cfg.balance_poll_seconds > 0:
            self._balance_task = asyncio.create_task(self._balance_loop())

        await self._sync_position_from_balance()
        bal = await self.rt.get_balances()
        logger.info(
            "LIVE %s | USDC=%.1f pos=%s | RSI%d %s/%s k=%.0f",
            market.slug,
            bal.usdc.balance if bal.usdc else 0,
            self._position.value,
            self.strat_cfg.rsi_period,
            self.strat_cfg.cross_up_level,
            self.strat_cfg.cross_down_level,
            self.strat_cfg.size,
        )

    async def stop(self) -> None:
        self._running = False
        for t in (self._rollover_task, self._balance_task):
            if t:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        await self.rt.stop()
        await self.resolver.close()
        logger.info(
            "Stop | signals up/down=%s/%s trades=%s",
            self.signals_up,
            self.signals_down,
            self.trades_executed,
        )

    async def _resolve_market(self) -> MarketInfo:
        mcfg = self.live_cfg.market
        if mcfg.slug.strip():
            return await self.resolver.resolve_by_slug(mcfg.slug.strip())
        m = await self.resolver.find_active_latest(mcfg.slug_pattern)
        if not m:
            raise ValueError(f"Aktif piyasa yok: {mcfg.slug_pattern}")
        return m

    def _market_end_time(self, market: MarketInfo) -> datetime | None:
        if market.end_date:
            try:
                return datetime.fromisoformat(market.end_date.replace("Z", "+00:00"))
            except ValueError:
                pass
        return market_end_from_slug(market.slug)

    def _minutes_left(self) -> float | None:
        if not self._market_end_dt:
            return None
        return minutes_to_expiry(datetime.now(timezone.utc), self._market_end_dt)

    def _is_live(self) -> bool:
        return (
            self.trading_cfg.mode == TradingMode.LIVE
            and not self.trading_cfg.dry_run
        )

    def _yes_pos(self) -> float:
        return self.state.positions.get(TokenLabel.YES, 0.0)

    def _no_pos(self) -> float:
        return self.state.positions.get(TokenLabel.NO, 0.0)

    async def _held_size(self, token_id: str, *, force_rest: bool = False) -> float:
        """WS pozisyonu yeterse onu kullan; sadece force_rest veya WS bos ise REST."""
        label = self.state.token_label(token_id)
        if not force_rest and label:
            local = self.state.positions.get(label, 0.0)
            if local >= MIN_POSITION_SHARES:
                return local
        bal = await self.rt.get_balances([token_id])
        tb = bal.tokens.get(token_id)
        v = round_down_shares(tb.balance if tb else 0.0)
        if label:
            self.state.positions[label] = v
        return v

    async def _sync_position_from_balance(self) -> None:
        """Live: motor pozisyonunu CLOB bakiyesiyle esitle (baslangic/rollover)."""
        if not self._market or not self._is_live():
            return
        bal = await self.rt.get_balances(
            [self._market.yes_token_id, self._market.no_token_id]
        )
        yes_sz = round_down_shares(
            bal.tokens.get(self._market.yes_token_id).balance
            if bal.tokens.get(self._market.yes_token_id)
            else 0.0
        )
        no_sz = round_down_shares(
            bal.tokens.get(self._market.no_token_id).balance
            if bal.tokens.get(self._market.no_token_id)
            else 0.0
        )
        self.state.positions[TokenLabel.YES] = yes_sz
        self.state.positions[TokenLabel.NO] = no_sz
        if yes_sz > MIN_POSITION_SHARES and no_sz > MIN_POSITION_SHARES:
            logger.warning(
                "Cift taraf bakiye: YES=%.2f NO=%.2f", yes_sz, no_sz
            )
        if yes_sz > MIN_POSITION_SHARES:
            self._position = Side.UP
        elif no_sz > MIN_POSITION_SHARES:
            self._position = Side.DOWN
        else:
            self._position = Side.FLAT

    async def _flatten_token(
        self, token: TokenLabel, bar: BarSnapshot, label: str
    ) -> bool:
        """Bakiyede kalan token'i sat (motor FLAT olsa bile)."""
        if not self._market or not self._is_live():
            return True
        token_id = (
            self._market.yes_token_id
            if token == TokenLabel.YES
            else self._market.no_token_id
        )
        size = self.state.positions.get(token, 0.0)
        if size < MIN_POSITION_SHARES:
            self._needs_flatten.discard(token)
            return True
        t0 = time.monotonic_ns()
        res = await self.rt.sell_fast(token_id, token, size)
        elapsed_ms = (time.monotonic_ns() - t0) / 1_000_000

        # API basari = trade gerceklesti. State.positions WS ile arka planda guncellenir.
        if not res.success:
            self._needs_flatten.add(token)
            logger.warning(
                "[%s] SELL fail %.0fms (queue retry): %s",
                label,
                elapsed_ms,
                (res.error or "")[:80],
            )
            return False

        self._needs_flatten.discard(token)
        if token == TokenLabel.YES and self._position == Side.UP:
            self._position = Side.FLAT
        elif token == TokenLabel.NO and self._position == Side.DOWN:
            self._position = Side.FLAT
        logger.info("[%s] SELL x %.1f | %.0fms", label, size, elapsed_ms)
        return True

    async def _retry_orphans(self) -> None:
        """Her market WS tick'inde: pending orphan'lari hemen yeniden sat."""
        if not self._needs_flatten or not self._is_live():
            return
        now = time.monotonic()
        bar = self._current_bar_snapshot()
        for token in list(self._needs_flatten):
            current = self.state.positions.get(token, 0.0)
            if current < MIN_POSITION_SHARES:
                self._needs_flatten.discard(token)
                continue
            last_t = self._last_orphan_retry.get(token, 0.0)
            if now - last_t < ORPHAN_RETRY_INTERVAL:
                continue
            self._last_orphan_retry[token] = now
            await self._flatten_token(token, bar, f"RETRY_{token.value}")

    async def _prepare_flip_to(self, target: Side, bar: BarSnapshot) -> bool:
        """UP/DOWN flip: karsi taraf orphan + mevcut pozisyonu kapat."""
        if not self._is_live() or not self._market:
            return True
        yes_sz = self._yes_pos()
        no_sz = self._no_pos()

        if target == Side.UP:
            if no_sz >= MIN_POSITION_SHARES:
                if not await self._flatten_token(TokenLabel.NO, bar, "FLIP_NO"):
                    return False
            if self._position == Side.DOWN:
                if not await self._close_position(TokenLabel.NO, bar, "SELL_DOWN"):
                    return False
        elif target == Side.DOWN:
            if yes_sz >= MIN_POSITION_SHARES:
                if not await self._flatten_token(TokenLabel.YES, bar, "FLIP_YES"):
                    return False
            if self._position == Side.UP:
                if not await self._close_position(TokenLabel.YES, bar, "SELL_UP"):
                    return False
        return True

    async def _attach_market_and_wire(self, market: MarketInfo) -> None:
        """WS bagla; rollover sonrasi strateji handler'ini yeniden bagla."""
        await self.rt.attach_market(market)
        self._market = market
        self._market_end_dt = self._market_end_time(market)
        self._resolution_phase = ""
        self._wire_market_events()

    def _wire_market_events(self) -> None:
        """attach_market yeni WS olusturur; RSI/resolution handler kaybolmasin."""
        ws = self.rt.market_ws
        if not ws:
            return
        rt_handler = self.rt._on_market_event

        async def _chained(event_type: str, data: dict) -> None:
            await self._on_market_event(event_type, data)
            await rt_handler(event_type, data)

        ws.on_event = _chained

    def _current_bar_snapshot(self) -> BarSnapshot:
        yes_book = self.state.book_for(TokenLabel.YES)
        no_book = self.state.book_for(TokenLabel.NO)
        ym = yes_book.mid() or 0.5
        nm = no_book.mid() or 0.5
        return BarSnapshot(
            time=datetime.now(timezone.utc),
            yes_mid=ym,
            yes_bid=yes_book.best_bid,
            yes_ask=yes_book.best_ask,
            no_mid=nm,
            no_bid=no_book.best_bid,
            no_ask=no_book.best_ask,
            rsi=self.rsi_stream.last_rsi or 50.0,
            cross_up=False,
            cross_down=False,
        )

    async def _on_market_event(self, event_type: str, data: dict) -> None:
        if event_type not in ("book", "price_change", "best_bid_ask", "last_trade_price"):
            return
        async with self._trade_lock:
            await self._check_resolution_risk()
            # Pozisyon ile bakiye uyusmuyorsa RSI sinyali beklemeden hemen sat
            await self._retry_orphans()
            await self._evaluate_rsi()

    def _position_mid(self) -> float | None:
        if self._position == Side.UP:
            return self.state.book_for(TokenLabel.YES).mid()
        if self._position == Side.DOWN:
            return self.state.book_for(TokenLabel.NO).mid()
        return None

    async def _check_resolution_risk(self) -> None:
        """Son P dk: dusuk olasilikli pozisyonu sat; yuksek olasilikli tut."""
        left = self._minutes_left()
        if left is None:
            return

        res = self.strat_cfg.resolution
        slug = self._market.slug if self._market else ""
        phase = resolution_phase(left, res, slug)
        self.state.minutes_to_expiry = max(0.0, left)

        if phase != self._resolution_phase:
            self._resolution_phase = phase
            if phase in ("block_entries", "force_close") and self._is_live():
                await self.rt.gateway.cancel_all()

        if self._position == Side.FLAT:
            return

        mid = self._position_mid()
        do_close, why = should_force_close(mid, phase, res)

        if do_close:
            await self._force_close_all(why)

    async def _force_close_all(self, reason: str) -> None:
        if self._position == Side.FLAT or not self._market:
            return
        bar = self._current_bar_snapshot()
        side_before = self._position
        token, label = (
            (TokenLabel.YES, "RES_SELL_UP")
            if self._position == Side.UP
            else (TokenLabel.NO, "RES_SELL_DOWN")
        )
        await self._close_position(token, bar, label)
        if self._is_live():
            await self.rt.gateway.cancel_all()
        logger.warning("RES CLOSE %s | %s", reason, side_before.value)

    def _new_entries_blocked(self) -> bool:
        left = self._minutes_left()
        if left is None or not self._market:
            return False
        return left <= entries_window_minutes(
            self._market.slug, self.strat_cfg.resolution
        )

    def _resolution_blocks_flip(self) -> bool:
        if self._position == Side.FLAT or not self._market:
            return False
        left = self._minutes_left()
        if left is None:
            return False
        phase = resolution_phase(
            left, self.strat_cfg.resolution, self._market.slug
        )
        if phase not in ("force_close", "expired"):
            return False
        mid = self._position_mid()
        do_close, why = should_force_close(
            mid, phase, self.strat_cfg.resolution
        )
        return not do_close and why.startswith("hold_gte")

    async def _evaluate_rsi(self) -> None:
        if not self._market:
            return

        yes_book = self.state.book_for(TokenLabel.YES)
        no_book = self.state.book_for(TokenLabel.NO)
        yes_mid = yes_book.mid()
        if yes_mid is None:
            return

        now = datetime.now(timezone.utc)
        bar = self.rsi_stream.update(
            now,
            yes_mid,
            yes_bid=yes_book.best_bid,
            yes_ask=yes_book.best_ask,
            no_mid=no_book.mid(),
            no_bid=no_book.best_bid,
            no_ask=no_book.best_ask,
        )
        if bar is None:
            return

        if bar.cross_up:
            self.signals_up += 1
            await self._handle_cross_up(bar)
        elif bar.cross_down:
            self.signals_down += 1
            await self._handle_cross_down(bar)

    def _cooldown_ok(self, now: datetime) -> bool:
        if self.strat_cfg.cooldown_seconds <= 0 or self._last_trade_at is None:
            return True
        return (now - self._last_trade_at).total_seconds() >= self.strat_cfg.cooldown_seconds

    def _entry_allowed(self, token: TokenLabel, bar: BarSnapshot) -> bool:
        px = bar.yes_mid if token == TokenLabel.YES else bar.no_mid
        return is_entry_price_safe(px, self.strat_cfg)

    async def _handle_cross_up(self, bar: BarSnapshot) -> None:
        if not self._cooldown_ok(bar.time):
            return
        if self._position == Side.UP and self.strat_cfg.ignore_same_side_signal:
            return
        if self._resolution_blocks_flip():
            return
        if not await self._prepare_flip_to(Side.UP, bar):
            return
        if self._position == Side.FLAT:
            if self._new_entries_blocked():
                return
            await self._open_position(TokenLabel.YES, Side.UP, bar, "BUY_UP")

    async def _handle_cross_down(self, bar: BarSnapshot) -> None:
        if not self._cooldown_ok(bar.time):
            return
        if self._position == Side.DOWN and self.strat_cfg.ignore_same_side_signal:
            return
        if self._resolution_blocks_flip():
            return
        if not await self._prepare_flip_to(Side.DOWN, bar):
            return
        want_down = self.strat_cfg.enter_down_when_flat_on_cross_down
        if self._position == Side.FLAT and want_down:
            if self._new_entries_blocked():
                return
            await self._open_position(TokenLabel.NO, Side.DOWN, bar, "BUY_DOWN")

    async def _close_position(
        self, token: TokenLabel, bar: BarSnapshot, label: str
    ) -> bool:
        if not self._market:
            return True
        if self._is_live():
            return await self._flatten_token(token, bar, label)
        # paper
        if self._position == Side.FLAT:
            return True
        return True

    async def _open_position(
        self,
        token: TokenLabel,
        new_pos: Side,
        bar: BarSnapshot,
        label: str,
    ) -> bool:
        if not self._market or self._position != Side.FLAT:
            return False
        if not self._entry_allowed(token, bar):
            return False

        token_id = (
            self._market.yes_token_id
            if token == TokenLabel.YES
            else self._market.no_token_id
        )

        if not self._is_live():
            return False  # paper modu artik desteklenmiyor (live odakli)

        # Karsi taraf orphan kontrolu (User WS pozisyonundan)
        yes_sz = self._yes_pos()
        no_sz = self._no_pos()
        if new_pos == Side.UP and no_sz >= MIN_POSITION_SHARES:
            if not await self._flatten_token(TokenLabel.NO, bar, "PRE_NO"):
                return False
        if new_pos == Side.DOWN and yes_sz >= MIN_POSITION_SHARES:
            if not await self._flatten_token(TokenLabel.YES, bar, "PRE_YES"):
                return False

        t0 = time.monotonic_ns()
        res = await self.rt.buy_fast(token_id, token, self.strat_cfg.size)
        elapsed_ms = (time.monotonic_ns() - t0) / 1_000_000

        # API success = CLOB orderID dondu -> FAK match oldu. WS event beklemiyoruz.
        # state.positions WS apply_fill ile arka planda guncellenir (dedup'lu).
        if res.success:
            self._position = new_pos
            self._last_trade_at = bar.time
            self.trades_executed += 1
            logger.info(
                "[%s] BUY x %.1f | pos=%s | rsi=%.0f | %.0fms",
                label,
                res.size or self.strat_cfg.size,
                new_pos.value,
                bar.rsi,
                elapsed_ms,
            )
            return True

        logger.info(
            "[%s] BUY FAIL %.0fms: %s",
            label,
            elapsed_ms,
            (res.error or "")[:80] or "no fill",
        )
        return False

    async def _on_fill(self, data: dict) -> None:
        """User WS uzerinden fill geldiginde state esitlenir."""
        fill = data.get("fill")
        if not isinstance(fill, Fill):
            return
        if fill.token == TokenLabel.YES:
            if self.state.positions.get(TokenLabel.YES, 0.0) >= MIN_POSITION_SHARES:
                self._position = Side.UP
            else:
                self._position = Side.FLAT if self._no_pos() < MIN_POSITION_SHARES else Side.DOWN
        elif fill.token == TokenLabel.NO:
            if self.state.positions.get(TokenLabel.NO, 0.0) >= MIN_POSITION_SHARES:
                self._position = Side.DOWN
            else:
                self._position = Side.FLAT if self._yes_pos() < MIN_POSITION_SHARES else Side.UP

    async def _on_user_trade(self, data: dict) -> None:
        # WS state zaten apply_fill ile guncellendi; ek log gerekmez
        pass

    async def _balance_loop(self) -> None:
        """Periyodik dogrulama (60s) — User WS'ten kacan birikim varsa duzelt."""
        while self._running:
            try:
                await asyncio.sleep(self.live_cfg.balance_poll_seconds)
                await self._sync_position_from_balance()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Balance poll: %s", e)

    async def _rollover_loop(self) -> None:
        while self._running:
            try:
                await self._check_rollover()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Rollover: %s", e)
            await asyncio.sleep(self.live_cfg.market.rollover_check_seconds)

    async def _check_rollover(self) -> None:
        market = self._market
        if not market or not market.end_date:
            return
        try:
            end_dt = datetime.fromisoformat(market.end_date.replace("Z", "+00:00"))
        except ValueError:
            return
        minutes_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 60.0
        if minutes_left > 0:
            self.state.minutes_to_expiry = minutes_left
            return

        logger.info("Piyasa bitti -> rollover")
        async with self._trade_lock:
            res = self.strat_cfg.resolution
            phase = "expired"
            mid = self._position_mid()
            do_close, why = should_force_close(mid, phase, res)
            if do_close:
                await self._force_close_all(f"rollover_{why}")
            elif self._position != Side.FLAT and self._is_live():
                token = (
                    TokenLabel.YES if self._position == Side.UP else TokenLabel.NO
                )
                bar = self._current_bar_snapshot()
                await self._close_position(token, bar, "ROLLOVER")
            self._position = Side.FLAT
            if self._is_live():
                await self.rt.gateway.cancel_all()

        nxt = await self.resolver.find_next_by_pattern(
            self.live_cfg.market.slug_pattern, market.slug
        )
        if not nxt:
            nxt = await self.resolver.find_active_latest(
                self.live_cfg.market.slug_pattern
            )
        if not nxt or nxt.slug == market.slug:
            return

        self.rsi_stream = RsiLiveStream(self.strat_cfg)
        self._position = Side.FLAT
        await self._attach_market_and_wire(nxt)
        await self._sync_position_from_balance()
        logger.info("Rollover -> %s", nxt.slug)
