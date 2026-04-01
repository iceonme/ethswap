#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V9.3-Innovation Candidate
最小改进版候选策略：
1. 不改动当前运行中的 eth_swap_v93.py
2. 只调整买卖条件与趋势模块职责
3. 用于离线回测和后续候选比较
"""

from types import SimpleNamespace
from typing import List

from core.types import Side
from .eth_swap_v93 import V93Strategy


class V93CandidateStrategy(V93Strategy):
    """
    最小改进版：
    - 层决定区域，不再硬编码某层只能买/只能卖
    - 趋势模块不再决定是否持仓，只轻度调节仓位大小
    - 保持均值回归主骨架
    """

    def _update_dynamic_rsi_thresholds(self, atr: float, price: float, trend: int):
        # 固定阈值，避免趋势模块改写主交易逻辑
        self.rsi_oversold = 25.0
        self.rsi_overbought = 85.0
        self.rsi_exit_oversold = 40.0
        self.rsi_exit_overbought = 70.0

    def _size_by_trend(self, base_size: float, trend: int, direction: str) -> float:
        """趋势只负责仓位力度，不负责方向决策"""
        if direction == 'long':
            if trend == 1:
                return base_size * 1.25
            if trend == -1:
                return base_size * 0.75
        elif direction == 'short':
            if trend == -1:
                return base_size * 1.25
            if trend == 1:
                return base_size * 0.75
        return base_size

    def _make_signal(self, side: str, size: float, price: float, ts, reason: str, pos_side: str):
        return SimpleNamespace(
            symbol=self.symbol,
            side=Side.BUY if side == 'buy' else Side.SELL,
            size=float(size),
            price=price,
            timestamp=ts,
            meta={'reason': reason, 'posSide': pos_side},
            reason=reason,
        )

    def _grid_trading(self, layer: int, rsi: float, has_long: bool, has_short: bool,
                      current_price: float, current_time, trend: int,
                      context) -> List:
        """
        区域逻辑：
        - 低区(-1,0): 偏开多 / 平空
        - 中区(1): 观望
        - 高区(2,3): 偏开空 / 平多
        """
        signals = []

        in_low_zone = layer in (-1, 0)
        in_mid_zone = layer == 1
        in_high_zone = layer in (2, 3)

        # 低区：优先平空，其次试多
        if in_low_zone:
            if has_short and rsi <= self.rsi_exit_oversold:
                for symbol, pos in context.positions.items():
                    if symbol == self.symbol and pos.size < 0:
                        signals.append(self._make_signal(
                            'buy', abs(pos.size), current_price, current_time,
                            f'close_short_low_zone_l{layer}', 'short'
                        ))
                        return signals

            if not has_long and rsi <= self.rsi_oversold:
                size = self._size_by_trend(0.2 * self.current_leverage, trend, 'long')
                signals.append(self._make_signal(
                    'buy', size, current_price, current_time,
                    f'open_long_low_zone_l{layer}', 'long'
                ))
                return signals

        # 中区：暂不主动开新仓
        elif in_mid_zone:
            return signals

        # 高区：优先平多，其次试空
        elif in_high_zone:
            if has_long and rsi >= self.rsi_exit_overbought:
                for symbol, pos in context.positions.items():
                    if symbol == self.symbol and pos.size > 0:
                        signals.append(self._make_signal(
                            'sell', pos.size, current_price, current_time,
                            f'close_long_high_zone_l{layer}', 'long'
                        ))
                        return signals

            if not has_short and rsi >= self.rsi_overbought:
                size = self._size_by_trend(0.2 * self.current_leverage, trend, 'short')
                signals.append(self._make_signal(
                    'sell', size, current_price, current_time,
                    f'open_short_high_zone_l{layer}', 'short'
                ))
                return signals

        return signals

    def _no_grid_trading(self, rsi: float, has_long: bool, has_short: bool,
                         current_price: float, current_time, trend: int,
                         context) -> List:
        """越界模式保守处理：趋势只调仓位，不决定方向"""
        signals = []

        if has_long and rsi >= self.rsi_exit_overbought:
            for symbol, pos in context.positions.items():
                if symbol == self.symbol and pos.size > 0:
                    signals.append(self._make_signal(
                        'sell', pos.size, current_price, current_time,
                        'no_grid_close_long', 'long'
                    ))
                    return signals

        if has_short and rsi <= self.rsi_exit_oversold:
            for symbol, pos in context.positions.items():
                if symbol == self.symbol and pos.size < 0:
                    signals.append(self._make_signal(
                        'buy', abs(pos.size), current_price, current_time,
                        'no_grid_close_short', 'short'
                    ))
                    return signals

        if not has_long and rsi <= self.rsi_oversold:
            size = self._size_by_trend(0.15 * self.current_leverage, trend, 'long')
            signals.append(self._make_signal(
                'buy', size, current_price, current_time,
                'no_grid_open_long', 'long'
            ))
            return signals

        if not has_short and rsi >= self.rsi_overbought:
            size = self._size_by_trend(0.15 * self.current_leverage, trend, 'short')
            signals.append(self._make_signal(
                'sell', size, current_price, current_time,
                'no_grid_open_short', 'short'
            ))
            return signals

        return signals
