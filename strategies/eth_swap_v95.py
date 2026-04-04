#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V9.3-Innovation - ETH永续合约动态网格策略
版本: 2025-03-22 (创新版)
端口: 5090

核心创新:
- 3层网格架构: 实体3层(低/中/高) + 虚拟2层(极值缓冲)
- 动态RSI阈值: 基于波动率自适应调整
- 网格归并: 顺势归并保留敞口，逆势止损
- 无网格模式: 网格失效时RSI极值直接交易
- 动态杠杆: 1x-3x (基于ATR自适应)
- 双向交易: 做多(中层下半部) + 做空(低层上半部)
"""

import os
import json
import time
import os
import math
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple, Optional, Any
import logging
import sys

# 环境定义
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, 'data')
LOG_DIR = os.path.join(BASE_DIR, 'logs')

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# 导入基础组件
from infra.okx.client import OKXClient
from .base import BaseStrategy
from core.types import Signal, MarketData, StrategyContext, Side, OrderType
logger = logging.getLogger('V9.5-Innovation')
logger.setLevel(logging.INFO)

# 日志处理器由外部配置或使用默认

# 从策略中彻底移除本地 OKXAPI 类定义，由外部引擎提供 Executor 或注入 Client


class V95Strategy(BaseStrategy):
    """V9.3-Innovation 策略核心 - 3层网格+动态RSI+网格归并"""

    def __init__(self, config: Dict = None, **kwargs):
        # 统一配置处理
        self.config = config or kwargs
        super().__init__(name="V9.3-Innovation-v1.1", **self.config)

        self.name = "V9.5-Innovation" # v9.5：按层加仓 + 远层优先平仓 + 禁止对冲
        self.symbol = self.config.get('symbol', 'ETH-USDT-SWAP')
        self.last_run_time = None

        # 网格状态 - 3层实体 + 2层虚拟
        self.entity_grids: List[float] = []  # 3层实体: [低, 中, 高]
        self.virtual_grids: List[float] = []  # 2层虚拟: [极低, 极高]
        self.grid_top = 0.0
        self.grid_bottom = 0.0
        self.grid_middle = 0.0

        # 动态RSI阈值
        self.rsi_oversold = 25.0        # 实体层做多入场
        self.rsi_overbought = 85.0      # 实体层做空入场
        self.rsi_exit_oversold = 40.0   # 平空
        self.rsi_exit_overbought = 70.0 # 平多
        self.virtual_rsi_oversold = 25.0   # 虚拟低层做多入场（保持原严度）
        self.virtual_rsi_overbought = 85.0 # 虚拟高层做空入场（保持原严度）

        # 网格归并状态
        self.merged_grid_level: Optional[int] = None  # 归并后的网格层级
        self.position_entry_price: float = 0.0  # 入场价格
        self.position_direction: int = 0  # 1=多, -1=空, 0=无

        # 运行时状态
        self.last_reset_day: Optional[datetime.date] = None
        self.breakout_triggered = False
        self.breakout_time: Optional[datetime] = None
        self.current_leverage = self.config.get('leverage_base', 3.0)
        self.base_position_eth = self.config.get('base_position_eth', 0.2) # 作为静默/备份值
        self.use_auto_sizing = True # 默认开启动态分仓
        self.positions_snapshot: Dict[str, Any] = {}

        # 数据缓存
        self.price_history: List[float] = []
        self.df_history: pd.DataFrame = pd.DataFrame()

        # 统计
        self.trade_count = 0
        self.daily_reset_count = 0
        self.layer_positions: Dict[str, Dict[int, Dict[str, Any]]] = {'long': {}, 'short': {}}
        self.occupied_long_layers = set()  # 追踪已开单的长仓层级（由 layer_positions 派生）
        self.occupied_short_layers = set() # 追踪已开单的短仓层级（由 layer_positions 派生）

        # 状态持久化追踪
        self._last_saved_equity: float = 0.0  # 上次写入的权益值，用于权益变化阈值判断

        # ====== 黑天鹅/单边行情保护 ======
        self.blackswan_level = 0          # 0=正常, 1=预警, 2=保护, 3=熔断
        self.blackswan_direction = 0      # 1=上涨行情, -1=下跌行情, 0=无
        self.blackswan_thresholds = (4.0, 6.0, 8.0)  # 预警/保护/熔断 百分比 (ETH适配)
        self.blackswan_recovery = 3.0     # 预警解除阈值 %
        self.blackswan_halved = False     # 保护级是否已执行减仓
        self.blackswan_last_log_time = 0  # 日志节流
        self.breakout_reset_count = 0
        self.last_periodic_reset_hour = -1  # 记录上一次进行温和重置的小时
        self._last_no_grid_log_time = 0     # 用于抑制无网格模式的重复刷屏日志
        self._last_intercept_log_time = 0   # 用于抑制网格内单向持仓拦截的重复刷屏日志

        # 网格计算记录（仅记录最近一次计算时间，不做定时强制重置）
        self.last_grid_calc_time: Optional[datetime] = None

        # 稳定性增强
        self.last_candle_ts: Optional[datetime] = None
        self.last_trade_time: float = 0.0 # 上次交易时间戳
        self.cooldown_seconds = 60 # 60秒冷却
        self.last_entry_time: float = 0.0  # 上次开仓时间戳（防重复开仓）
        self.min_entry_interval: float = 30.0  # 最小开仓间隔30秒

        # 状态数据，用于 get_status
        self.status_data: Dict[str, Any] = {}

        # 尝试加载历史网格状态 (如果是手动重置后的重启，则跳过加载以强制重算)
        self.force_reset_grid = self.config.get('force_reset_grid', False)
        if not self.force_reset_grid:
            self._load_grid_state()
        else:
            logger.info("检测到手动重置指令：将跳过旧状态加载，强制重新计算网格")

        logger.info(f"V9.5-Innovation 初始化完成 | 端口: {self.config.get('port', 5090)}")

    def _refresh_occupied_layers(self):
        self.occupied_long_layers = set(int(layer) for layer, bucket in self.layer_positions.get('long', {}).items() if bucket.get('size', 0.0) > 1e-8)
        self.occupied_short_layers = set(int(layer) for layer, bucket in self.layer_positions.get('short', {}).items() if bucket.get('size', 0.0) > 1e-8)

    def _layer_tag(self, side: str, layer: int) -> str:
        if side == 'long':
            return '正常层' if layer in (-1, 0) else '外挂层'
        return '正常层' if layer in (2, 3) else '外挂层'

    def _get_layer_bucket(self, side: str, layer: int, create: bool = False) -> Optional[Dict[str, Any]]:
        side_buckets = self.layer_positions.setdefault(side, {})
        layer = int(layer)
        bucket = side_buckets.get(layer)
        if bucket is None and create:
            bucket = {'size': 0.0, 'avg_price': 0.0, 'tag': self._layer_tag(side, layer)}
            side_buckets[layer] = bucket
        return bucket

    def _add_layer_position(self, side: str, layer: int, size: float, avg_price: float, tag: Optional[str] = None):
        if size <= 1e-8:
            return
        bucket = self._get_layer_bucket(side, layer, create=True)
        old_size = float(bucket.get('size', 0.0) or 0.0)
        new_size = old_size + float(size)
        if new_size <= 1e-8:
            self.layer_positions.get(side, {}).pop(int(layer), None)
        else:
            old_avg = float(bucket.get('avg_price', 0.0) or 0.0)
            if old_size > 1e-8 and old_avg > 0 and avg_price > 0:
                bucket['avg_price'] = (old_size * old_avg + float(size) * float(avg_price)) / new_size
            elif avg_price > 0:
                bucket['avg_price'] = float(avg_price)
            bucket['size'] = new_size
            bucket['tag'] = tag or bucket.get('tag') or self._layer_tag(side, int(layer))
        self._refresh_occupied_layers()

    def _reduce_layer_position(self, side: str, layer: int, size: float) -> float:
        if size <= 1e-8:
            return 0.0
        bucket = self._get_layer_bucket(side, layer, create=False)
        if not bucket:
            return 0.0
        reduce_size = min(float(size), float(bucket.get('size', 0.0) or 0.0))
        remain = float(bucket.get('size', 0.0) or 0.0) - reduce_size
        if remain <= 1e-8:
            self.layer_positions.get(side, {}).pop(int(layer), None)
        else:
            bucket['size'] = remain
        self._refresh_occupied_layers()
        return reduce_size

    def _choose_farthest_layer(self, side: str, current_layer: Optional[int]) -> Optional[int]:
        side_buckets = self.layer_positions.get(side, {})
        if not side_buckets:
            return None
        anchor = current_layer if current_layer is not None else (3 if side == 'long' else 0)
        return max(
            side_buckets.keys(),
            key=lambda layer: (abs(int(layer) - anchor), abs(int(layer)))
        )

    def _allocate_external_layer(self, side: str) -> int:
        occupied = set(int(layer) for layer in self.layer_positions.get(side, {}).keys())
        if side == 'long':
            layer = -2
            while layer in occupied:
                layer -= 1
            return layer
        layer = 4
        while layer in occupied:
            layer += 1
        return layer

    def _serialize_layer_positions(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        payload: Dict[str, Dict[str, Dict[str, Any]]] = {'long': {}, 'short': {}}
        for side in ('long', 'short'):
            for layer, bucket in self.layer_positions.get(side, {}).items():
                if bucket.get('size', 0.0) <= 1e-8:
                    continue
                payload[side][str(int(layer))] = {
                    'size': float(bucket.get('size', 0.0) or 0.0),
                    'avg_price': float(bucket.get('avg_price', 0.0) or 0.0),
                    'tag': bucket.get('tag') or self._layer_tag(side, int(layer))
                }
        return payload

    def _deserialize_layer_positions(self, payload: Any) -> Dict[str, Dict[int, Dict[str, Any]]]:
        data: Dict[str, Dict[int, Dict[str, Any]]] = {'long': {}, 'short': {}}
        if not isinstance(payload, dict):
            return data
        for side in ('long', 'short'):
            side_payload = payload.get(side, {})
            if not isinstance(side_payload, dict):
                continue
            for raw_layer, raw_bucket in side_payload.items():
                if not isinstance(raw_bucket, dict):
                    continue
                try:
                    layer = int(raw_layer)
                    size = float(raw_bucket.get('size', 0.0) or 0.0)
                    avg_price = float(raw_bucket.get('avg_price', 0.0) or 0.0)
                except (TypeError, ValueError):
                    continue
                if size <= 1e-8:
                    continue
                data[side][layer] = {
                    'size': size,
                    'avg_price': avg_price,
                    'tag': raw_bucket.get('tag') or self._layer_tag(side, layer)
                }
        return data

    def _extract_fill_meta(self, fill_data: Any) -> Dict[str, Any]:
        meta = getattr(fill_data, 'meta', None)
        if isinstance(fill_data, dict):
            meta = fill_data.get('meta', meta)
        return meta if isinstance(meta, dict) else {}

    def _extract_fill_layer(self, fill_data: Any) -> Optional[int]:
        meta = self._extract_fill_meta(fill_data)
        for key in ('close_layer', 'level'):
            value = meta.get(key)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    pass

        reason = str(meta.get('reason', '') or '')
        if 'layer-1' in reason:
            return -1
        if 'layer0' in reason:
            return 0
        if 'layer2' in reason:
            return 2
        if 'layer3' in reason:
            return 3
        if 'no_grid_long_entry' in reason:
            return -2
        if 'no_grid_short_entry' in reason:
            return 4

        marker = '_L'
        if marker in reason:
            suffix = reason.rsplit(marker, 1)[-1]
            digits = []
            for ch in suffix:
                if ch in '-0123456789':
                    digits.append(ch)
                else:
                    break
            if digits:
                try:
                    return int(''.join(digits))
                except ValueError:
                    return None
        return None

    def _build_layer_positions_from_context(self, context: Optional[StrategyContext]) -> Dict[str, Dict[int, Dict[str, Any]]]:
        rebuilt: Dict[str, Dict[int, Dict[str, Any]]] = {'long': {}, 'short': {}}
        if not context or not hasattr(context, 'positions') or not context.positions:
            return rebuilt

        for pos in context.positions.values():
            if pos.symbol != self.symbol or abs(pos.size) < 1e-8:
                continue
            side = 'long' if pos.size > 0 else 'short'
            avg_price = float(getattr(pos, 'avg_price', 0.0) or 0.0)
            level = getattr(pos, 'level', None)
            if level is None and avg_price > 0 and self.entity_grids and len(self.entity_grids) >= 4 and len(self.virtual_grids) >= 2:
                level = self._price_to_layer(avg_price, self.entity_grids, self.virtual_grids)
            if level is None:
                level = self._allocate_external_layer(side)
            layer = int(level)
            rebuilt[side][layer] = {
                'size': abs(float(pos.size)),
                'avg_price': avg_price,
                'tag': self._layer_tag(side, layer)
            }
        return rebuilt

    def _sync_layer_positions_from_context(self, context: Optional[StrategyContext]):
        rebuilt = self._build_layer_positions_from_context(context)
        if rebuilt.get('long') or rebuilt.get('short') or not (context and getattr(context, 'positions', None)):
            self.layer_positions = rebuilt
        self._refresh_occupied_layers()

    def _remap_layer_positions_after_grid_reset(self):
        if not self.layer_positions or not self.entity_grids or len(self.entity_grids) < 4 or len(self.virtual_grids) < 2:
            self._refresh_occupied_layers()
            return

        remapped: Dict[str, Dict[int, Dict[str, Any]]] = {'long': {}, 'short': {}}
        for side in ('long', 'short'):
            buckets = sorted(self.layer_positions.get(side, {}).items(), key=lambda item: item[0])
            for old_layer, bucket in buckets:
                size = float(bucket.get('size', 0.0) or 0.0)
                avg_price = float(bucket.get('avg_price', 0.0) or 0.0)
                if size <= 1e-8:
                    continue
                mapped_layer = self._price_to_layer(avg_price, self.entity_grids, self.virtual_grids) if avg_price > 0 else None
                if side == 'long' and mapped_layer in (-1, 0):
                    new_layer = int(mapped_layer)
                elif side == 'short' and mapped_layer in (2, 3):
                    new_layer = int(mapped_layer)
                else:
                    new_layer = self._allocate_external_layer(side) if not remapped[side] else (
                        min(remapped[side].keys()) - 1 if side == 'long' else max(remapped[side].keys()) + 1
                    )
                    if side == 'long' and new_layer >= -1:
                        new_layer = self._allocate_external_layer(side)
                    if side == 'short' and new_layer <= 3:
                        new_layer = self._allocate_external_layer(side)
                remapped[side][new_layer] = {
                    'size': size,
                    'avg_price': avg_price,
                    'tag': self._layer_tag(side, new_layer)
                }
                if new_layer != old_layer:
                    logger.info(f"[层级重映射] {side} | {old_layer} -> {new_layer} | size: {size:.4f} | avg: {avg_price:.2f}")
        self.layer_positions = remapped
        self._refresh_occupied_layers()

    def on_start(self):
        """引擎启动时调用"""
        logger.info(f"策略 {self.name} 已启动")

    def on_fill(self, fill_data: Dict[str, Any], context: Optional[StrategyContext] = None):
        """
        成交回调 — 外部引擎在每笔成交后调用此方法。
        立即同步持仓层级 + 权益快照到 v95_state.json，解决重启后持仓丢失问题。

        fill_data 示例: {'symbol': 'ETH-USD-SWAP', 'side': 'BUY', 'size': 0.5,
                          'price': 1800.0, 'order_id': 'xxx', ...}
        """
        try:
            # 1. 从 context 同步当前持仓信息到内部变量
            long_pos = None
            short_pos = None
            total_eth = 0.0
            long_position_eth = 0.0
            short_position_eth = 0.0

            if context and hasattr(context, 'positions') and context.positions:
                for pos in context.positions.values():
                    if pos.symbol != self.symbol:
                        continue
                    if pos.size > 1e-8:
                        long_pos = pos
                        total_eth += pos.size
                        long_position_eth += pos.size
                    elif pos.size < -1e-8:
                        short_pos = pos
                        total_eth += abs(pos.size)
                        short_position_eth += abs(pos.size)

            self._long_avg_price = long_pos.avg_price if long_pos and hasattr(long_pos, 'avg_price') else 0.0
            self._short_avg_price = short_pos.avg_price if short_pos and hasattr(short_pos, 'avg_price') else 0.0
            self._total_position_eth = total_eth
            self._long_position_eth = long_position_eth
            self._short_position_eth = short_position_eth

            # 2. 从 context 获取当前权益
            equity = 0.0
            cash = 0.0
            unrealized_pnl = 0.0
            realized_pnl = 0.0
            position_market_value = 0.0
            if context and hasattr(context, 'meta'):
                equity = context.meta.get('total_equity', 0.0)
                cash = context.meta.get('cash', 0.0)
                unrealized_pnl = context.meta.get('unrealized_pnl', 0.0)
                realized_pnl = context.meta.get('realized_pnl', 0.0)
                position_market_value = context.meta.get('position_market_value', 0.0)
            elif context and hasattr(context, 'total_value'):
                equity = context.total_value

            self._saved_equity = equity
            self._saved_cash = cash
            self._saved_unrealized_pnl = unrealized_pnl
            self._saved_realized_pnl = realized_pnl
            self._saved_position_market_value = position_market_value
            self._saved_mark_price = float(context.current_prices.get(self.symbol, fill_data.filled_price if hasattr(fill_data, 'filled_price') else 0.0)) if context and hasattr(context, 'current_prices') else (float(fill_data.filled_price) if hasattr(fill_data, 'filled_price') else 0.0)

            # 3. 权益变化阈值判断（变化 > 10 USD 才写盘，避免频繁 IO）
            equity_delta = abs(equity - self._last_saved_equity) if self._last_saved_equity > 0 else float('inf')

            fill_meta = self._extract_fill_meta(fill_data)
            fill_side = str(fill_meta.get('posSide', '') or '').lower()
            fill_layer = self._extract_fill_layer(fill_data)
            fill_price = float(getattr(fill_data, 'filled_price', 0.0) or (fill_data.get('filled_price', 0.0) if isinstance(fill_data, dict) else 0.0))
            fill_size = float(abs(getattr(fill_data, 'filled_size', 0.0) or (fill_data.get('filled_size', 0.0) if isinstance(fill_data, dict) else 0.0)))
            fill_reason = str(fill_meta.get('reason', '') or '')

            if fill_side in ('long', 'short') and fill_layer is not None and fill_size > 1e-8:
                if 'entry' in fill_reason or '开' in fill_reason:
                    self._add_layer_position(fill_side, fill_layer, fill_size, fill_price)
                elif ('exit' in fill_reason or 'close' in fill_reason or '平' in fill_reason
                      or 'blackswan' in fill_reason or 'stop_loss' in fill_reason):
                    reduced = self._reduce_layer_position(fill_side, fill_layer, fill_size)
                    if reduced + 1e-8 < fill_size:
                        self._sync_layer_positions_from_context(context)
                else:
                    self._sync_layer_positions_from_context(context)
            else:
                self._sync_layer_positions_from_context(context)

            if equity_delta > 10.0 or self._last_saved_equity == 0.0:
                self._save_grid_state()
                self._last_saved_equity = equity
                logger.info(f"[on_fill] 成交同步 | 持仓: {total_eth:.4f} ETH | 权益: {equity:.2f} USD | Δequity: {equity_delta:.2f} | 已保存状态")
            else:
                # 权益变化小，只同步内存层信息
                logger.info(f"[on_fill] 成交同步 | 持仓: {total_eth:.4f} ETH | 权益: {equity:.2f} USD | Δequity: {equity_delta:.2f} | 跳过写入")

        except Exception as e:
            logger.error(f"[on_fill] 成交同步状态失败: {e}")

    def _sync_occupied_layers_from_context(self, context: StrategyContext):
        """
        根据 context.positions 重新同步 occupied_long_layers / occupied_short_layers。
        用于在 on_fill 后纠正层级状态。
        """
        try:
            self._sync_layer_positions_from_context(context)
        except Exception as e:
            logger.warning(f"[_sync_occupied_layers_from_context] 同步层级失败: {e}")

    def _price_to_layer(self, price: float, grids: List[float], virtual_grids: List[float]) -> Optional[int]:
        """根据价格计算所在层级 (-2~4)"""
        if price < grids[0]:
            return -2 if price < virtual_grids[0] else -1
        elif price < grids[1]:
            return 0
        elif price < grids[2]:
            return 1
        elif price < grids[3]:
            return 2
        elif price < virtual_grids[1]:
            return 3
        else:
            return 4

    def _update_buffer(self, data: MarketData):
        """更新内部数据缓存 (对齐分钟级别，避免盘中抖动)"""
        # 如果是同分钟的数据，替换最后一个点；如果是新分钟，追加点
        if self.last_candle_ts == data.timestamp:
            if self.price_history:
                self.price_history[-1] = data.close
        else:
            self.price_history.append(data.close)
            self.last_candle_ts = data.timestamp

        if len(self.price_history) > 400:
            self.price_history = self.price_history[-400:]

        # 维护 DataFrame 用于 ATR 计算等 (同理对齐)
        if not self.df_history.empty and self.df_history.index[-1] == data.timestamp:
            self.df_history.iloc[-1] = [data.open, data.high, data.low, data.close, data.volume]
        else:
            new_row = pd.DataFrame([{
                'open': data.open, 'high': data.high,
                'low': data.low, 'close': data.close, 'vol': data.volume
            }], index=[data.timestamp])
            self.df_history = pd.concat([self.df_history, new_row]).tail(400)

    def on_data(self, data: MarketData, context: StrategyContext) -> List[Signal]:
        """分层架构核心：处理每一根K线并返回信号"""
        # 1. 更新缓存
        self._update_buffer(data)

        if len(self.df_history) < 20:
            return []

        current_price = data.close
        current_time = data.timestamp
        signals = []

        # 2. 计算指标
        rsi = self.calculate_rsi(self.price_history)
        atr = self.calculate_atr(self.df_history)
        confidence = self.lstm_confidence(self.price_history)
        trend = 1 if confidence >= 0.55 else (-1 if confidence <= -0.55 else 0)

        # 3. 动态RSI阈值 (保留原有动态阈值能力，并叠加LSTM置信度门控)
        self._update_dynamic_rsi_thresholds(atr, current_price, confidence)

        # 4. 动态杠杆 (通过 context 传递给引擎)
        self.calculate_dynamic_leverage_for_engine(atr, current_price, context)

        # 5. 初始化或重置网格
        reset_needed, reset_window = self.check_reset_conditions(current_price, current_time)
        
        # 5.0 黑天鹅保护检查 (在网格重置之前)
        blackswan_signals = self._check_blackswan(current_price, current_time, context)
        if blackswan_signals:
            signals.extend(blackswan_signals)
            # 保护级恢复: 保护/熔断级在4h重置时自动降级
        
        # 保护级以上暂停网格重置
        if self._blackswan_blocks_reset():
            reset_needed = False
            if reset_window:
                logger.info(f"[黑天鹅] 保护级以上，跳过网格重置")
        
        # 2026-03-26 修复：重置点后的第一次计算，必须等待数据积累到 350 根（接近 6 小时窗口）之后
        # 避免在预热初期（如仅 20 根 K 线时）就计算并锁死了一个无效的小范围网格
        should_init = not self.entity_grids and len(self.df_history) >= 350
        
        if should_init or reset_needed:
            # 用户要求重置时持仓保留原样
            if context.positions:
                logger.info(f"网格重置触发 | 原因: {'初始化' if should_init else '突破重置'} | 数据量: {len(self.df_history)} | 持仓状态: 保留")

            # 执行网格计算 (自适应 6h 或 4h 窗口)
            self.calculate_grids(self.df_history, window_hours=reset_window)

            # 网格重置后，保留可映射的正常层，不可映射仓位迁移到外挂层
            if context.positions and self.entity_grids:
                if not self.layer_positions.get('long') and not self.layer_positions.get('short'):
                    self._sync_layer_positions_from_context(context)
                self._remap_layer_positions_after_grid_reset()

            # 网格重置后，检查当前价格是否仍在虚拟层外
            if len(self.virtual_grids) >= 2:
                is_outside = current_price < self.virtual_grids[0] or current_price > self.virtual_grids[1]
                if is_outside:
                    self.breakout_triggered = True
                    self.breakout_time = current_time
                    self._no_grid_triggered_in_breakout = False
                    logger.info(f"网格计算完成，但价格仍在外围 ({current_price:.2f}) | 网格顶部: {self.virtual_grids[1]:.2f} | 继续观察")
                else:
                    self.breakout_triggered = False
                    self.breakout_time = None
                    self._no_grid_triggered_in_breakout = False
                    logger.info(f"网格计算并对齐完成 | 区间: [{self.grid_bottom:.2f} ↔ {self.grid_top:.2f}]")

            # 治本：重置后强制进入冷却期，防止同一tick重复开仓
            self.last_trade_time = time.time()
            self.last_entry_time = time.time()
            logger.info(f"重置后冷却 | 交易冷却{self.cooldown_seconds}秒 + 开仓冷却{self.min_entry_interval}秒")

        # 5.1 获取当前层 (3层实体架构) - 放在冷却期前，以便日志显示最新层级
        layer = self.get_current_layer(current_price)

        # 5.2 交易冷却检查
        time_now = time.time()
        if time_now - self.last_trade_time < self.cooldown_seconds:
             # 在冷却期内，跳过普通交易逻辑，但允许黑天鹅保护信号通过
             if signals:
                 self.last_trade_time = time_now
                 self._print_enhanced_logs(rsi, current_price, trend, confidence, layer, context)
                 return signals
             
             self._print_enhanced_logs(rsi, current_price, trend, confidence, layer, context)
             return []


        # 7. 获取持仓状态 (从 context) 并同步层级占用状态
        is_warmup = bool(getattr(context, 'meta', {}) and context.meta.get('warmup'))
        has_long = False
        has_short = False
        for pos in context.positions.values():
            if pos.symbol == self.symbol:
                if pos.size > 0: has_long = True
                if pos.size < 0: has_short = True
        
        # 自动同步：如果持仓消失，则清空对应的层级占用状态
        # 2026-04-03 修复：warmup 阶段使用的是空持仓 mock context，不能拿来覆盖真实账户状态。
        if not is_warmup:
            if not has_long:
                if self.occupied_long_layers:
                    logger.info(f"[同步] 多仓已清空，释放已占用的层级: {list(self.occupied_long_layers)}")
                    self.layer_positions['long'] = {}
                    self._refresh_occupied_layers()
            if not has_short:
                if self.occupied_short_layers:
                    logger.info(f"[同步] 空仓已清空，释放已占用的层级: {list(self.occupied_short_layers)}")
                    self.layer_positions['short'] = {}
                    self._refresh_occupied_layers()

        # 8. 核心交易逻辑：出场优先 + 区域入场
        # 8.1 统一出场逻辑 (已禁用: 2026-04-01, 与网格层平仓冲突)
        # exit_signals = self._check_exit_signals(rsi, current_price, current_time, layer, context)
        # signals.extend(exit_signals)

        # 8.2 熔断级检查 (仅限制新开仓)
        if self.blackswan_level >= 3:
            if signals:
                self.last_trade_time = time_now
            self._print_enhanced_logs(rsi, current_price, trend, confidence, layer, context)
            return signals

        # 8.3 入场逻辑 (如果已经触发平项，则跳过本次循环的入场检查，避免同根K线反复摩擦)
        if not signals:
            if layer is None:
                # 预警级以上暂停无网格入场
                if self.blackswan_level < 1:
                    entry_signals = self._no_grid_trading(rsi, has_long, has_short, current_price, current_time, trend, confidence, context)
                    signals.extend(entry_signals)
            else:
                # 正常网格交易 (包含实体层与虚拟层)
                entry_signals = self._grid_trading(layer, rsi, has_long, has_short, current_price, current_time, trend, confidence, context)
                signals.extend(entry_signals)

        # 更新状态并记录冷却时间（如果有信号）
        if signals:
            self.last_trade_time = time_now

        self._print_enhanced_logs(rsi, current_price, trend, confidence, layer, context)
        return signals

    def _print_enhanced_logs(self, rsi, current_price, trend, confidence, layer, context: StrategyContext):
        """打印用户要求的增强型诊断与心跳日志 (每分钟一次)"""
        # 1. 检查是否到达 60 秒间隔
        if not hasattr(self, '_last_heartbeat_time') or (time.time() - self._last_heartbeat_time >= 60):
            now_str = datetime.now().strftime("%H:%M:%S")
            grid_desc = f"[{self.grid_bottom:.1f} - {self.grid_top:.1f}]" if self.grid_top > 0 else "[未初始化]"
            
            # 计算距离触发的 RSI 差距
            if rsi < 50:
                dist = f"距做多还差 {rsi - self.rsi_oversold:.1f}" if rsi > self.rsi_oversold else "满足做多RSI"
            else:
                dist = f"距做空还差 {self.rsi_overbought - rsi:.1f}" if rsi < self.rsi_overbought else "满足做空RSI"

            # 1. 策略诊断内容
            layer_name = f"L{layer}" if layer is not None else "外"
            abs_conf = abs(confidence)
            if abs_conf >= 0.55:
                conf_band = "强趋势可开仓"
            elif abs_conf >= 0.50:
                conf_band = "观望禁开新仓"
            else:
                conf_band = "纯网格均值回归"
            diag_msg = f"[策略观察] {now_str} | 价格: {current_price:.2f} | RSI: {rsi:.1f} ({dist}) | 置信度: {confidence:+.3f}({conf_band}) | 层级: {layer_name} | 网格: {grid_desc}"
            logger.info(diag_msg)

            # 2. 状态摘要
            equity = context.meta.get('total_equity', context.total_value)
            initial = context.meta.get('initial_equity', equity)
            profit_pct = ((equity / initial) - 1) * 100 if initial > 0 else 0.0
            pos_size = sum(abs(p.size) for p in context.positions.values() if p.symbol == self.symbol)
            
            summary_msg = f"[系统状态] 持仓: {pos_size:.3f} ETH | 总权益: {equity:.2f} | 收益: {profit_pct:+.2f}%"
            logger.info(summary_msg)
            
            # 更新上次打印时间
            self._last_heartbeat_time = time.time()

        # 3. 实时状态更新 (必须每 tick 执行，用于 Dashboard 和逻辑判断)
        self.last_rsi = rsi
        self.last_layer = layer if layer is not None else -1
        self.last_trend = trend
        self.status_data = {
            'rsi': rsi,
            'layer': layer,
            'trend': trend,
            'confidence': confidence,
            'current_price': current_price
        }

    def _update_dynamic_rsi_thresholds(self, atr: float, price: float, confidence: float):
        """动态RSI阈值：保留原有RSI动态阈值能力，并用LSTM置信度做二次门控。

        机制：
        - abs(confidence) >= 0.55 → 强趋势档，可开新仓
        - 0.50 <= abs(confidence) < 0.55 → 观望档，不开新仓
        - abs(confidence) < 0.50 → 纯网格/均值回归档
        """
        atr_pct = (atr / price * 100.0) if price > 0 else 0.0

        # 先保留原有“动态”能力：由波动率给出基础阈值
        base_long = 30.0
        base_short = 80.0
        if atr_pct >= 2.5:
            base_long = 35.0
            base_short = 75.0
        elif atr_pct >= 1.5:
            base_long = 32.0
            base_short = 78.0

        abs_conf = abs(confidence)

        if abs_conf >= 0.55:
            self.rsi_oversold = min(base_long + 5.0, 40.0)
            self.rsi_overbought = max(base_short - 5.0, 70.0)
            self.rsi_exit_oversold = min(self.rsi_oversold + 10.0, 50.0)
            self.rsi_exit_overbought = max(self.rsi_overbought - 10.0, 60.0)
            self.virtual_rsi_oversold = max(self.rsi_oversold - 5.0, 30.0)
            self.virtual_rsi_overbought = min(self.rsi_overbought + 5.0, 80.0)
        elif abs_conf >= 0.50:
            self.rsi_oversold = base_long
            self.rsi_overbought = base_short
            self.rsi_exit_oversold = min(base_long + 10.0, 45.0)
            self.rsi_exit_overbought = max(base_short - 10.0, 65.0)
            self.virtual_rsi_oversold = max(base_long - 2.0, 30.0)
            self.virtual_rsi_overbought = min(base_short + 2.0, 80.0)
        else:
            self.rsi_oversold = base_long
            self.rsi_overbought = base_short
            self.rsi_exit_oversold = min(base_long + 10.0, 45.0)
            self.rsi_exit_overbought = max(base_short - 10.0, 65.0)
            self.virtual_rsi_oversold = self.rsi_oversold
            self.virtual_rsi_overbought = self.rsi_overbought

    def _handle_grid_merge(self, context: StrategyContext, current_price: float, trend: int) -> List[Signal]:
        """网格归并：顺势归并保留敞口，逆势止损"""
        signals = []

        for pos in context.positions.values():
            if pos.size == 0 or pos.symbol != self.symbol:
                continue

            is_long = pos.size > 0
            entry_price = pos.avg_price if hasattr(pos, 'avg_price') else current_price

            # 顺势归并：多+涨→归并，空+跌→归并
            if is_long and trend == 1 and current_price > entry_price:
                # 顺势做多，保留仓位，更新入场价为当前网格层
                self.position_direction = 1
                self.position_entry_price = current_price
                logger.info(f"网格归并(顺势) | 多仓保留 | 入场价更新: {entry_price:.2f} -> {current_price:.2f}")
                continue
            elif not is_long and trend == -1 and current_price < entry_price:
                # 顺势做空，保留仓位
                self.position_direction = -1
                self.position_entry_price = current_price
                logger.info(f"网格归并(顺势) | 空仓保留 | 入场价更新: {entry_price:.2f} -> {current_price:.2f}")
                continue
            else:
                # 逆势止损：多+跌或空+涨→平仓
                side = Side.SELL if is_long else Side.BUY
                signals.append(Signal(
                    symbol=pos.symbol, side=side, size=abs(pos.size),
                    price=current_price, timestamp=datetime.now(timezone.utc),
                    meta={'reason': 'grid_merge_stop_loss', 'posSide': 'long' if is_long else 'short'}
                ))
                self.position_direction = 0
                logger.info(f"网格归并(逆势) | {'多' if is_long else '空'}仓止损 | 价格: {current_price:.2f}")

        return signals

    def _no_grid_trading(self, rsi: float, has_long: bool, has_short: bool,
                         current_price: float, current_time: datetime, trend: int,
                         confidence: float, context: StrategyContext) -> List[Signal]:
        """无网格模式：两小时观察期内交易，仅一次，条件写死

        触发条件（与原逻辑一致）：
        - 弱趋势：做多 RSI <= 28，做空 RSI >= 80
        - 强趋势：做多 RSI <= 25，做空 RSI >= 75
        - 两小时观察期内仅触发一次（买入或卖出）
        - 观察期结束计数归零
        """
        signals = []
        long_positions = [p for p in context.positions.values() if p.symbol == self.symbol and p.size > 1e-8]
        short_positions = [p for p in context.positions.values() if p.symbol == self.symbol and p.size < -1e-8]

        def _position_level(pos):
            level = getattr(pos, 'level', None)
            if level is not None:
                return level
            avg_price = getattr(pos, 'avg_price', current_price)
            return self._get_current_layer(avg_price)

        def _position_in_layer(positions, target_layer):
            if not positions or target_layer is None:
                return None
            for pos in positions:
                if _position_level(pos) == target_layer:
                    return pos
            return None

        # 为了避免每秒刷屏，设定 60 秒的节流输出标识
        should_log = False
        time_now = time.time()
        if time_now - self._last_no_grid_log_time >= 60:
            should_log = True
            self._last_no_grid_log_time = time_now

        # 检查是否在突破观察期内
        if not self.breakout_triggered or self.breakout_time is None:
            if should_log:
                logger.info(f"无网格观察 | 未在观察期内 | RSI: {rsi:.1f} | 价格: {current_price:.2f}")
            return signals

        # 检查观察期是否已结束（2小时）
        elapsed = (current_time - self.breakout_time).total_seconds()
        if elapsed >= 2 * 3600:
            if should_log:
                logger.info(f"无网格观察 | 观察期已结束 | RSI: {rsi:.1f} | 价格: {current_price:.2f}")
            return signals

        # 检查本观察期内是否已触发过
        if hasattr(self, '_no_grid_triggered_in_breakout') and self._no_grid_triggered_in_breakout:
            if should_log:
                logger.info(f"无网格交易 | 本观察期已触发过，跳过 | RSI: {rsi:.1f} | 价格: {current_price:.2f}")
            return signals

        # 置信度观望门控：0.50~0.55 禁止新开仓
        abs_conf = abs(confidence)
        if 0.50 <= abs_conf < 0.55:
            if should_log:
                logger.info(f"无网格观察 | 置信度观望区，禁止新开仓 | confidence: {confidence:+.3f}")
            return signals

        # 获取虚拟层边界
        if len(self.virtual_grids) < 2:
            if should_log:
                logger.info(f"无网格观察 | 虚拟层未初始化 | RSI: {rsi:.1f} | 价格: {current_price:.2f}")
            return signals

        virtual_low = self.virtual_grids[0]   # 虚拟低层
        virtual_high = self.virtual_grids[1]  # 虚拟高层

        # 根据趋势确定阈值（与原逻辑一致，无网格比虚拟层更严5点）
        if abs(trend) >= 1:  # 强趋势
            long_threshold = 25.0   # 做多更宽松
            short_threshold = 80.0  # 做空更严格（虚拟层75，无网格80）
            trend_name = '强趋势'
        else:  # 弱趋势/震荡
            long_threshold = 28.0   # 做多更严格
            short_threshold = 85.0  # 做空更宽松（虚拟层80，无网格85）
            trend_name = '弱趋势'

        # 计算基础仓位（固定比例，不加杠杆加成）
        equity = context.meta.get('total_equity', context.total_value)
        no_grid_size = (equity / 5.0) / current_price if current_price > 0 else self.base_position_eth * 0.5
        no_grid_size = min(no_grid_size, 0.6)  # 上限扩大到0.6 ETH（漏网之鱼捡大一点）

        next_long_external = self._allocate_external_layer('long')
        next_short_external = self._allocate_external_layer('short')

        # 下半部做多：跌破虚拟低层 + RSI阈值 + 该区域未开仓
        if current_price < virtual_low and rsi <= long_threshold and next_long_external not in self.occupied_long_layers and not has_short and not _position_in_layer(long_positions, next_long_external):
            signals.append(Signal(
                symbol=self.symbol, side=Side.BUY, size=no_grid_size,
                price=current_price, timestamp=current_time,
                meta={'reason': 'no_grid_long_entry', 'posSide': 'long', 'level': next_long_external}
            ))
            self._no_grid_triggered_in_breakout = True
            self.last_entry_time = time.time()
            logger.info(f"无网格交易 | 下半部做多 | 外挂层: L{next_long_external} | {trend_name} | RSI: {rsi:.1f} <= {long_threshold:.0f} | 价格: {current_price:.2f} | 观察期剩余: {((2*3600-elapsed)/60):.0f}分钟")

        # 上半部做空：涨破虚拟高层 + RSI阈值 + 该区域未开仓
        elif current_price > virtual_high and rsi >= short_threshold and next_short_external not in self.occupied_short_layers and not has_long and not _position_in_layer(short_positions, next_short_external):
            signals.append(Signal(
                symbol=self.symbol, side=Side.SELL, size=no_grid_size,
                price=current_price, timestamp=current_time,
                meta={'reason': 'no_grid_short_entry', 'posSide': 'short', 'level': next_short_external}
            ))
            self._no_grid_triggered_in_breakout = True
            self.last_entry_time = time.time()
            logger.info(f"无网格交易 | 上半部做空 | 外挂层: L{next_short_external} | {trend_name} | RSI: {rsi:.1f} >= {short_threshold:.0f} | 价格: {current_price:.2f} | 观察期剩余: {((2*3600-elapsed)/60):.0f}分钟")

        else:
            if should_log:
                logger.info(f"无网格观察 | 条件未满足 | {trend_name} | RSI: {rsi:.1f} | 做多阈值: {long_threshold:.0f} | 做空阈值: {short_threshold:.0f}")

        return signals

    def _grid_trading(self, layer: int, rsi: float, has_long: bool, has_short: bool,
                      current_price: float, current_time: datetime, trend: int,
                      confidence: float, context: StrategyContext) -> List[Signal]:
        """按层级拦截逻辑：同层拦截，异层放行"""
        signals = []

        # ========== 平仓逻辑（v9.5 新增：平最远层） ==========
        # 从 _check_exit_signals 提取，按层判断 + 只平离当前层最远的持仓
        long_positions = [p for p in context.positions.values() if p.symbol == self.symbol and p.size > 1e-8]
        short_positions = [p for p in context.positions.values() if p.symbol == self.symbol and p.size < -1e-8]

        def _position_in_layer(positions, target_layer):
            """检查是否存在同向持仓已在目标层级，返回该持仓或None"""
            if not positions or target_layer is None:
                return None
            for pos in positions:
                lvl = getattr(pos, 'level', None)
                if lvl is None:
                    avg_price = getattr(pos, 'avg_price', current_price)
                    lvl = self._get_current_layer(avg_price)
                if lvl == target_layer:
                    return pos
            return None

        # 网格中线/L1缓冲层：L1不再承担开平仓，仅作缓冲观察
        layer1_mid = (self.entity_grids[1] + self.entity_grids[2]) / 2 if self.entity_grids and len(self.entity_grids) >= 4 else 0
        grid_mid = layer1_mid

        # 平多阈值（超买）：layer2中间(+5)，layer3最容易(基准)；L1不再平多
        exit_ob_layer2 = self.rsi_exit_overbought + 5.0
        exit_ob_layer3 = self.rsi_exit_overbought

        # 平空阈值（超卖）：layer0中间(-5)，layer-1最容易(基准)；L1不再平空
        exit_os_layer0 = self.rsi_exit_oversold - 5.0
        exit_os_layerN1 = self.rsi_exit_oversold

        eff_layer = layer if layer is not None else (3 if current_price > self.grid_top else 0)

        # 平多检查（仅 layer 2/3 或网格外高位；L1不再平多）
        if long_positions and eff_layer in (2, 3):
            rsi_threshold = exit_ob_layer3 if eff_layer == 3 else exit_ob_layer2
            if rsi >= rsi_threshold:
                close_layer = self._choose_farthest_layer('long', eff_layer)
                close_bucket = self.layer_positions.get('long', {}).get(close_layer) if close_layer is not None else None
                if close_bucket:
                    close_size = float(close_bucket.get('size', 0.0) or 0.0)
                    close_avg_price = float(close_bucket.get('avg_price', 0.0) or 0.0)
                    estimated_fee = current_price * close_size * 0.0005
                    gross_profit = (current_price - close_avg_price) * close_size
                    net_profit = gross_profit - estimated_fee
                    min_required_profit = estimated_fee * 3.0
                    if net_profit >= min_required_profit:
                        signals.append(Signal(
                            symbol=self.symbol, side=Side.SELL, size=close_size,
                            price=current_price, timestamp=current_time,
                            meta={'reason': f'exit_long_farthest_L{close_layer}', 'posSide': 'long', 'close_layer': close_layer}
                        ))
                        logger.info(f"[平仓] 平多(最远层) | RSI: {rsi:.1f} >= {rsi_threshold:.1f} | 平仓: {close_size:.4f} ETH | 实际平仓层: L{close_layer} | 触发层: L{eff_layer} | 净利保护通过 {net_profit:.2f} >= {min_required_profit:.2f}")
                    else:
                        logger.info(f"[平仓拦截] 平多(最远层) | 实际平仓层: L{close_layer} | 触发层: L{eff_layer} | 净利保护未通过 {net_profit:.2f} < {min_required_profit:.2f}")

        # 平空检查（仅 layer -1/0 或网格外低位；L1不再平空）
        if short_positions and eff_layer in (-1, 0):
            rsi_threshold = exit_os_layerN1 if eff_layer == -1 else exit_os_layer0
            if rsi <= rsi_threshold:
                close_layer = self._choose_farthest_layer('short', eff_layer)
                close_bucket = self.layer_positions.get('short', {}).get(close_layer) if close_layer is not None else None
                if close_bucket:
                    close_size = float(close_bucket.get('size', 0.0) or 0.0)
                    close_avg_price = float(close_bucket.get('avg_price', 0.0) or 0.0)
                    estimated_fee = current_price * close_size * 0.0005
                    gross_profit = (close_avg_price - current_price) * close_size
                    net_profit = gross_profit - estimated_fee
                    min_required_profit = estimated_fee * 3.0
                    if net_profit >= min_required_profit:
                        signals.append(Signal(
                            symbol=self.symbol, side=Side.BUY, size=close_size,
                            price=current_price, timestamp=current_time,
                            meta={'reason': f'exit_short_farthest_L{close_layer}', 'posSide': 'short', 'close_layer': close_layer}
                        ))
                        logger.info(f"[平仓] 平空(最远层) | RSI: {rsi:.1f} <= {rsi_threshold:.1f} | 平仓: {close_size:.4f} ETH | 实际平仓层: L{close_layer} | 触发层: L{eff_layer} | 净利保护通过 {net_profit:.2f} >= {min_required_profit:.2f}")
                    else:
                        logger.info(f"[平仓拦截] 平空(最远层) | 实际平仓层: L{close_layer} | 触发层: L{eff_layer} | 净利保护未通过 {net_profit:.2f} < {min_required_profit:.2f}")
        # ========== 平仓逻辑结束 ==========

        # --- 入场逻辑 ---
        abs_conf = abs(confidence)
        allow_new_entry = not (0.50 <= abs_conf < 0.55)
        virtual_long_threshold = self.virtual_rsi_oversold
        virtual_short_threshold = self.virtual_rsi_overbought

        should_log_intercept = False
        time_now = time.time()
        if time_now - getattr(self, '_last_intercept_log_time', 0) >= 60:
            should_log_intercept = True
            self._last_intercept_log_time = time_now

        # --- 动态下单量计算 ---
        equity = context.meta.get('total_equity', context.total_value)
        base_size_with_lev = (equity / 5.0) / current_price if current_price > 0 else self.base_position_eth

        # 获取持仓描述用于日志
        long_pos = next((p for p in context.positions.values() if p.symbol == self.symbol and p.size > 0), None)
        short_pos = next((p for p in context.positions.values() if p.symbol == self.symbol and p.size < 0), None)
        
        pos_desc = "无持仓"
        if long_pos:
            pos_desc = f"做多 {long_pos.size:.4f} ETH (层级: {list(self.occupied_long_layers)})"
        elif short_pos:
            pos_desc = f"做空 {abs(short_pos.size):.4f} ETH (层级: {list(self.occupied_short_layers)})"

        # 1. 做多（仅 L-1 / L0；且必须进入各自层的下半部）
        if layer in (-1, 0):
            layer_lower_bound = self.virtual_grids[0] if layer == -1 and self.virtual_grids else self.entity_grids[0]
            layer_upper_bound = self.entity_grids[0] if layer == -1 else self.entity_grids[1]
            layer_mid = (layer_lower_bound + layer_upper_bound) / 2 if layer_upper_bound > layer_lower_bound else current_price

            if current_price > layer_mid:
                if should_log_intercept:
                    logger.info(f"  [位置拦截] 做多需进入层级 {layer} 的下半部 | price={current_price:.2f} > layer_mid={layer_mid:.2f}")
            elif has_short:
                if should_log_intercept:
                    logger.info(f"  [拦截开多] 价格 {current_price:.2f} 在做多区，但持有空仓，跳过对冲")
            elif layer in self.occupied_long_layers:
                if should_log_intercept:
                    logger.info(f"  [拦截开多] 价格 {current_price:.2f} 在层级 {layer}，但该层已开仓，禁止重复加仓")
            else:
                if time_now - self.last_entry_time < self.min_entry_interval:
                    if should_log_intercept:
                        logger.info(f"  [开仓冷却] 距上次开仓仅 {time_now - self.last_entry_time:.0f}秒，跳过")
                    return signals
                
                if not allow_new_entry:
                    if should_log_intercept:
                        logger.info(f"  [置信度门控] 当前 confidence={confidence:+.3f}，处于观望区，禁止开多")
                elif self._is_blackswan_blocked(side_is_buy=True):
                    if should_log_intercept:
                        logger.info(f"  [黑天鹅拦截] 禁止开多 | 级别: {self.blackswan_level}")
                elif layer == -1:
                    if rsi <= virtual_long_threshold:
                        size = base_size_with_lev
                        signals.append(Signal(
                            symbol=self.symbol, side=Side.BUY, size=size,
                            price=current_price, timestamp=current_time,
                            meta={'reason': 'layer-1_long_entry', 'posSide': 'long', 'level': layer}
                        ))
                        logger.info(f"入场信号 | 做多 (虚拟低层) | RSI: {rsi:.1f} | 价格: {current_price:.2f}")
                        self.last_entry_time = time_now
                else:
                    if rsi <= self.rsi_oversold:
                        size = base_size_with_lev
                        signals.append(Signal(
                            symbol=self.symbol, side=Side.BUY, size=size,
                            price=current_price, timestamp=current_time,
                            meta={'reason': 'layer0_long_entry', 'posSide': 'long', 'level': layer}
                        ))
                        logger.info(f"入场信号 | 做多 (层0) | RSI: {rsi:.1f} <= {self.rsi_oversold} | 价格: {current_price:.2f}")
                        self.last_entry_time = time_now

        # 2. 做空（仅 L2 / L3；且必须进入各自层的上半部）
        elif layer in (2, 3):
            layer_lower_bound = self.entity_grids[2] if layer == 3 else self.entity_grids[1]
            layer_upper_bound = self.virtual_grids[1] if layer == 3 and self.virtual_grids else self.entity_grids[3]
            layer_mid = (layer_lower_bound + layer_upper_bound) / 2 if layer_upper_bound > layer_lower_bound else current_price

            if current_price < layer_mid:
                if should_log_intercept:
                    logger.info(f"  [位置拦截] 做空需进入层级 {layer} 的上半部 | price={current_price:.2f} < layer_mid={layer_mid:.2f}")
            elif has_long:
                if should_log_intercept:
                    logger.info(f"  [拦截开空] 价格 {current_price:.2f} 在做空区，但持有多仓，跳过对冲")
            elif layer in self.occupied_short_layers:
                if should_log_intercept:
                    logger.info(f"  [拦截开空] 价格 {current_price:.2f} 在层级 {layer}，但该层已开仓，禁止重复加仓")
            else:
                if time_now - self.last_entry_time < self.min_entry_interval:
                    if should_log_intercept:
                        logger.info(f"  [开仓冷却] 距上次开仓仅 {time_now - self.last_entry_time:.0f}秒，跳过")
                    return signals
                
                if not allow_new_entry:
                    if should_log_intercept:
                        logger.info(f"  [置信度门控] 当前 confidence={confidence:+.3f}，处于观望区，禁止开空")
                elif self._is_blackswan_blocked(side_is_buy=False):
                    if should_log_intercept:
                        logger.info(f"  [黑天鹅拦截] 禁止开空 | 级别: {self.blackswan_level}")
                elif layer == 3:
                    if rsi >= virtual_short_threshold:
                        size = base_size_with_lev
                        signals.append(Signal(
                            symbol=self.symbol, side=Side.SELL, size=size,
                            price=current_price, timestamp=current_time,
                            meta={'reason': 'layer3_short_entry', 'posSide': 'short', 'level': layer}
                        ))
                        logger.info(f"入场信号 | 做空 (虚拟高层) | RSI: {rsi:.1f} | 价格: {current_price:.2f}")
                        self.last_entry_time = time_now
                else:
                    if rsi >= self.rsi_overbought:
                        size = base_size_with_lev
                        signals.append(Signal(
                            symbol=self.symbol, side=Side.SELL, size=size,
                            price=current_price, timestamp=current_time,
                            meta={'reason': 'layer2_short_entry', 'posSide': 'short', 'level': layer}
                        ))
                        logger.info(f"入场信号 | 做空 (层2) | RSI: {rsi:.1f} >= {self.rsi_overbought} | 价格: {current_price:.2f}")
                        self.last_entry_time = time_now

        return signals

    def _check_exit_signals(self, rsi: float, current_price: float, current_time: datetime,
                           layer: Optional[int], context: StrategyContext) -> List[Signal]:
        """统一全区间出场监测逻辑"""
        signals = []

        # 获取持仓 (支持双向语义)
        long_positions = [p for p in context.positions.values() if p.symbol == self.symbol and p.size > 1e-8]
        short_positions = [p for p in context.positions.values() if p.symbol == self.symbol and p.size < -1e-8]

        def _position_level(pos):
            level = getattr(pos, 'level', None)
            if level is not None:
                return level
            avg_price = getattr(pos, 'avg_price', current_price)
            return self._get_current_layer(avg_price)

        def _pick_farthest_position(positions, current_layer):
            if not positions:
                return None
            return max(
                positions,
                key=lambda pos: abs((_position_level(pos) if _position_level(pos) is not None else (current_layer or 0)) - (current_layer or 0))
            )

        long_pos = _pick_farthest_position(long_positions, layer)
        short_pos = _pick_farthest_position(short_positions, layer)

        # 阈值配置：L1 已退出平仓体系，现按两级结构执行
        # 平多超买: layer2(基准) < layer3(基准+5) —— 外层更难平
        # 平空超卖: layer0(基准-5) > layer-1(基准-10) —— 外层更难平
        exit_ob_layer1 = self.rsi_exit_overbought + 10.0  # layer1: 最难平（仅保留注释对照）
        exit_ob_layer2 = self.rsi_exit_overbought         # layer2: 内层先平
        exit_ob_layer3 = self.rsi_exit_overbought + 5.0   # layer3: 外层更难平

        exit_os_layer1 = self.rsi_exit_oversold - 10.0  # layer1: 最难平（仅保留注释对照）
        exit_os_layer0 = self.rsi_exit_oversold - 5.0   # layer0: 内层先平
        exit_os_layerN1 = self.rsi_exit_oversold - 10.0 # layer-1: 外层更难平

        # Layer1 中点：上下半层过滤（平多只在上半层，平空只在下半层）
        layer1_mid = (self.entity_grids[1] + self.entity_grids[2]) / 2 if self.entity_grids and len(self.entity_grids) >= 4 else 0

        # 1. 平多监测
        if long_pos:
            # 层级映射处理 (layer 为 None 时，如果价格高于中轴则参考高层阈值)
            eff_layer = layer
            if eff_layer is None and self.grid_top > 0:
                eff_layer = 3 if current_price > self.grid_top else 0
            
            # 多头平仓区：层 1, 2, 3 或网格外高位
            if eff_layer in (1, 2, 3):
                # Layer1 半层过滤：平多只在上半层（价格 >= 中点）
                if eff_layer == 1 and layer1_mid > 0 and current_price < layer1_mid:
                    pass  # 在L1下半层，不允许平多
                else:
                    if eff_layer == 1:
                        rsi_threshold = exit_ob_layer1
                    elif eff_layer == 2:
                        rsi_threshold = exit_ob_layer2
                    else:  # layer 3
                        rsi_threshold = exit_ob_layer3
                    if rsi >= rsi_threshold:
                        signals.append(Signal(
                            symbol=self.symbol, side=Side.SELL, size=abs(long_pos.size),
                            price=current_price, timestamp=current_time,
                            meta={'reason': f'exit_long_eff_layer{eff_layer}', 'posSide': 'long'}
                        ))
                        logger.info(f"[统一平仓] 平多信号 (层{eff_layer}) | RSI: {rsi:.1f} >= {rsi_threshold:.1f} | L1中点: {layer1_mid:.2f}")

        # 2. 平空监测
        if short_pos:
            eff_layer = layer
            if eff_layer is None and self.grid_bottom > 0:
                eff_layer = -1 if current_price < self.grid_bottom else 2
            
            # 空头平仓区：层 -1, 0, 1 或网格外低位
            if eff_layer in (-1, 0, 1):
                # Layer1 半层过滤：平空只在下半层（价格 <= 中点）
                if eff_layer == 1 and layer1_mid > 0 and current_price > layer1_mid:
                    pass  # 在L1上半层，不允许平空
                else:
                    if eff_layer == 1:
                        rsi_threshold = exit_os_layer1
                    elif eff_layer == 0:
                        rsi_threshold = exit_os_layer0
                    else:  # layer -1
                        rsi_threshold = exit_os_layerN1
                    if rsi <= rsi_threshold:
                        signals.append(Signal(
                            symbol=self.symbol, side=Side.BUY, size=abs(short_pos.size),
                            price=current_price, timestamp=current_time,
                            meta={'reason': f'exit_short_eff_layer{eff_layer}', 'posSide': 'short'}
                        ))
                        logger.info(f"[统一平仓] 平空信号 (层{eff_layer}) | RSI: {rsi:.1f} <= {rsi_threshold:.1f} | L1中点: {layer1_mid:.2f}")

        return signals

    def _get_current_layer(self, price: float) -> Optional[int]:
        """根据价格推断所在层级。"""
        if not self.entity_grids or len(self.entity_grids) < 4 or not self.virtual_grids or len(self.virtual_grids) < 2:
            return None
        if price < self.grid_bottom:
            return -1
        if self.entity_grids[0] <= price < self.entity_grids[1]:
            return 0
        if self.entity_grids[1] <= price < self.entity_grids[2]:
            return 1
        if self.entity_grids[2] <= price < self.entity_grids[3]:
            return 2
        if price >= self.grid_top:
            return 3
        return None

    def calculate_dynamic_leverage_for_engine(self, atr: float, price: float, context: StrategyContext):
        """动态杠杆逻辑 - 适配引擎"""
        atr_pct = atr / price * 100
        if atr_pct < 1.5: lev = 3.0
        elif atr_pct < 3.0: lev = 2.0
        else: lev = 1.0

        if lev != self.current_leverage:
            logger.info(f"杠杆调整请求: {self.current_leverage}x -> {lev}x (ATR: {atr_pct:.2f}%)")
            self.current_leverage = lev
            # 将杠杆调整请求放入 context.meta，LiveEngine 会捕获它
            context.meta['requested_leverage'] = lev

    def get_status(self) -> Dict:
        """返回给 Dashboard 的状态 (适配 V95Innovation)"""
        current_price = self.status_data.get('current_price', 0.0)
        rsi = self.status_data.get('rsi', 50.0)

        # 重新计算完整的 6 线网格供前端显示 (VH, P3, P2, P1, P0, VL)
        grid_prices = []
        if self.entity_grids and len(self.entity_grids) >= 4 and self.virtual_grids:
            full_grids = [
                self.virtual_grids[1],  # 极高
                self.entity_grids[3],   # P3 (高层顶/实体顶)
                self.entity_grids[2],   # P2
                self.entity_grids[1],   # P1
                self.entity_grids[0],   # P0 (底层底/实体底)
                self.virtual_grids[0]   # 极低
            ]
            grid_prices = [float(p) for p in full_grids]

        return {
            'name': self.name,
            'rsi': float(rsi),
            'layer': int(self.status_data.get('layer', -1) if self.status_data.get('layer') is not None else -1),
            'trend': int(self.status_data.get('trend', 0) if self.status_data.get('trend') is not None else 0),
            'leverage': float(self.current_leverage),
            'grid_range': [float(self.grid_bottom), float(self.grid_top)],
            'grid_prices': grid_prices,
            'grid_count': 3,
            'signal_text': self.status_data.get('signal_text', '等待中...'),
            'signal_color': self.status_data.get('signal_color', 'neutral'),
            'signal_strength': self.status_data.get('confidence', 0.0),
            'rsi_thresholds': {
                'oversold': float(self.rsi_oversold),
                'overbought': float(self.rsi_overbought)
            },
            'daily_reset': int(self.daily_reset_count),
            'breakout_reset': int(self.breakout_reset_count),
            'trade_count': int(self.trade_count),
            'judgment': self._generate_trading_judgment(rsi, current_price)
        }

    def _generate_trading_judgment(self, rsi: float, price: float) -> Dict[str, str]:
        """基于逻辑拼接生成交易判断文字 (覆盖全价格区间)"""
        # 安全检查：如果网格尚未初始化完成
        if len(self.entity_grids) < 4 or len(self.virtual_grids) < 2:
             return {"text": "策略正在初始化网格参数，请稍候...", "color": "neutral"}

        layer = self.get_current_layer(price)
        # 获取各档价格用于判断 (使用 entity_grids)
        p0, p1, p2, p3 = self.entity_grids[0], self.entity_grids[1], self.entity_grids[2], self.entity_grids[3]
        vl, vh = self.virtual_grids[0], self.virtual_grids[1]

        text = "策略监测中，正在校准参数..."
        color = "neutral"

        # 1. 超量程 (突破 VH / VL) - 第三刀后边界外不再新开仓
        if price > vh:
            if rsi > 80:
                text = f"价格极度向上突破 ({price:.1f}) 且 RSI 极度超买 ({rsi:.1f})，谨防短期剧烈回撤。第三刀后边界外不再新开仓，只观察或管理已有仓位。"
                color = "loss"
            else:
                text = f"价格向上突破虚拟防御层 ({price:.1f})，多头力量强劲。正在进行 2 小时趋势观察，等待网格自动重置。第三刀后边界外不再新开仓。"
                color = "primary"
        elif price < vl:
            text = f"价格向下深度破位 ({price:.1f})，市场恐慌情绪蔓延。正在进行下行趋势观察，第三刀后边界外不再新开仓，只观察或管理已有仓位。"
            color = "primary"

        # 2. 缓冲地带 (P3-VH 或 VL-P0) - 第三刀后已开放为虚拟交易区
        elif p3 < price <= vh:
             text = f"价格处于上虚拟交易区 ({price:.1f})。第三刀后此区域已开放做空职能，RSI 达到 {self.rsi_overbought + 5:.0f} 时可触发空单。"
             color = "neutral"
        elif vl <= price < p0:
             text = f"价格处于下虚拟交易区 ({price:.1f})。第三刀后此区域已开放做多职能，RSI 达到 {self.rsi_oversold - 5:.0f} 时可触发多单。"
             color = "neutral"
             
        # 3. 实体网格内部 (P0-P3)
        elif layer == 2: # P2-P3
            if rsi >= self.rsi_overbought:
                text = "价格进入高位压力区且 RSI 触发超买信号。策略准备开启空单（Layer 2 Short），预期博取向中轨回落的收益。"
                color = "loss"
            else:
                text = "价格处于高位震荡带，RSI 动能尚处于温和区域。建议持仓等待或空仓观察，暂不宜激进追高。"
                color = "neutral"
        elif layer == 0: # P0-P1
            if rsi <= self.rsi_oversold:
                text = "价格进入买入区且 RSI 触发超卖信号。策略准备开启多单（Layer 0 Long），目标是反弹至中轨区域。"
                color = "profit"
            else:
                text = "价格处于低位盘整带，下探动量尚未完全耗尽。策略将耐心等待 RSI 跌破 30 后的金叉反弹机会。"
                color = "neutral"
        elif layer == 1: # P1-P2
            text = "价格处于网格中心枢纽位。此区域多空博弈均衡，盈亏比不佳，策略将执行'休整'指令，等待价格向边缘移动。"
            color = "neutral"

        return {"text": text, "color": color}

    def _save_grid_state(self):
        """保存当前网格状态至文件 (最新值 + 增量历史)"""
        try:
            state = {
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'grid_top': float(self.grid_top),
                'grid_bottom': float(self.grid_bottom),
                'entity_grids': [float(p) for p in self.entity_grids],
                'virtual_grids': [float(p) for p in self.virtual_grids],
                'daily_reset_count': int(self.daily_reset_count),
                'breakout_triggered': self.breakout_triggered,
                'breakout_time': self.breakout_time.isoformat() if self.breakout_time else None,
                'no_grid_triggered': getattr(self, '_no_grid_triggered_in_breakout', False),
                'last_entry_time': self.last_entry_time,
                'occupied_long_layers': list(self.occupied_long_layers),
                'occupied_short_layers': list(self.occupied_short_layers),
                'layer_positions': self._serialize_layer_positions(),
                # --- 持仓快照（on_fill 成交后同步写入，重启恢复用） ---
                'long_avg_price': float(self._long_avg_price) if hasattr(self, '_long_avg_price') and self._long_avg_price else 0.0,
                'short_avg_price': float(self._short_avg_price) if hasattr(self, '_short_avg_price') and self._short_avg_price else 0.0,
                'long_position_eth': float(self._long_position_eth) if hasattr(self, '_long_position_eth') and self._long_position_eth else 0.0,
                'short_position_eth': float(self._short_position_eth) if hasattr(self, '_short_position_eth') and self._short_position_eth else 0.0,
                'total_position_eth': float(self._total_position_eth) if hasattr(self, '_total_position_eth') and self._total_position_eth else 0.0,
                'cash': float(self._saved_cash) if hasattr(self, '_saved_cash') else 0.0,
                'equity': float(self._saved_equity) if hasattr(self, '_saved_equity') and self._saved_equity else 0.0,
                'unrealized_pnl': float(self._saved_unrealized_pnl) if hasattr(self, '_saved_unrealized_pnl') else 0.0,
                'realized_pnl': float(self._saved_realized_pnl) if hasattr(self, '_saved_realized_pnl') else 0.0,
                'position_market_value': float(self._saved_position_market_value) if hasattr(self, '_saved_position_market_value') else 0.0,
                'mark_price': float(self._saved_mark_price) if hasattr(self, '_saved_mark_price') else 0.0,
            }

            # 1. 保存最新状态 (覆盖)
            state_file = os.path.join(DATA_DIR, 'v95_state.json')
            with open(state_file, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=4)

            # 2. 保存增量历史 (追加)
            history_file = os.path.join(DATA_DIR, 'v95_grid_history.json')
            history = []
            if os.path.exists(history_file):
                try:
                    with open(history_file, 'r', encoding='utf-8') as f:
                        history = json.load(f)
                except:
                    history = []

            history.append(state)
            # 保持历史记录不要无限大 (保留最近 150 条，约能覆盖 1-2 天的高频变动)
            if len(history) > 150:
                history = history[-150:]

            with open(history_file, 'w', encoding='utf-8') as f:
                json.dump(history, f, indent=4)

            logger.info("网格状态已持久化保存 (最新状态 + 增量历史)")
        except Exception as e:
            logger.error(f"保存网格状态失败: {e}")

    def restore_snapshot(self, state: Dict[str, Any]):
        """从对象快照恢复策略状态，不触发写盘"""
        if not state:
            return
            
        try:
            self.grid_top = float(state.get('grid_top', 0.0))
            self.grid_bottom = float(state.get('grid_bottom', 0.0))
            self.entity_grids = state.get('entity_grids', [])
            self.virtual_grids = state.get('virtual_grids', [])
            self.daily_reset_count = state.get('daily_reset_count', 0)
            self.breakout_triggered = state.get('breakout_triggered', False)
            bt_str = state.get('breakout_time')
            if bt_str:
                try:
                    self.breakout_time = datetime.fromisoformat(bt_str)
                except:
                    self.breakout_time = None
            self._no_grid_triggered_in_breakout = state.get('no_grid_triggered', False)
            self.last_entry_time = state.get('last_entry_time', 0.0)
            self.layer_positions = self._deserialize_layer_positions(state.get('layer_positions', {}))
            self.occupied_long_layers = set(state.get('occupied_long_layers', []))
            self.occupied_short_layers = set(state.get('occupied_short_layers', []))
            if self.layer_positions.get('long') or self.layer_positions.get('short'):
                self._refresh_occupied_layers()

            # --- 持仓快照恢复 ---
            self._long_avg_price = state.get('long_avg_price', 0.0)
            self._short_avg_price = state.get('short_avg_price', 0.0)
            self._long_position_eth = state.get('long_position_eth', state.get('total_position_eth', 0.0) if state.get('long_avg_price', 0.0) else 0.0)
            self._short_position_eth = state.get('short_position_eth', 0.0)
            self._total_position_eth = state.get('total_position_eth', 0.0)
            self._saved_equity = state.get('equity', 0.0)
            self._saved_cash = state.get('cash') if 'cash' in state else None
            self._saved_unrealized_pnl = state.get('unrealized_pnl', 0.0)
            self._saved_realized_pnl = state.get('realized_pnl', 0.0)
            self._saved_position_market_value = state.get('position_market_value', 0.0)
            self._saved_mark_price = state.get('mark_price', 0.0)
            if self._saved_cash is None and self._saved_equity:
                self._saved_cash = self._saved_equity - self._saved_unrealized_pnl
            self._last_saved_equity = self._saved_equity
            
            if self.entity_grids and self.virtual_grids:
                logger.info(f"[策略] 成功从快照恢复 | 权益: {self._saved_equity:.2f} USD")
        except Exception as e:
            logger.error(f"[策略] 恢复快照失败: {e}")

    def _load_grid_state(self):
        """从文件加载网格状态"""
        state_file = os.path.join(DATA_DIR, 'v95_state.json')
        if os.path.exists(state_file):
            try:
                with open(state_file, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                self.restore_snapshot(state)
                # 加载完后打个招呼
                if self.entity_grids:
                     logger.info(f"成功从文件加载网格状态 | 区间: [{self.grid_bottom:.2f} - {self.grid_top:.2f}]")
            except Exception as e:
                logger.error(f"直接加载状态文件失败: {e}")

    def get_paper_account_snapshot(self) -> Dict[str, float]:
        saved_equity = float(getattr(self, '_saved_equity', 0.0) or 0.0)
        saved_unrealized = float(getattr(self, '_saved_unrealized_pnl', 0.0) or 0.0)
        saved_cash = getattr(self, '_saved_cash', None)
        if saved_cash is None and saved_equity:
            saved_cash = saved_equity - saved_unrealized
        return {
            'cash': float(saved_cash or 0.0),
            'equity': saved_equity,
            'long_avg_price': float(getattr(self, '_long_avg_price', 0.0) or 0.0),
            'short_avg_price': float(getattr(self, '_short_avg_price', 0.0) or 0.0),
            'long_position_eth': float(getattr(self, '_long_position_eth', 0.0) or 0.0),
            'short_position_eth': float(getattr(self, '_short_position_eth', 0.0) or 0.0),
            'total_position_eth': float(getattr(self, '_total_position_eth', 0.0) or 0.0),
            'unrealized_pnl': saved_unrealized,
            'realized_pnl': float(getattr(self, '_saved_realized_pnl', 0.0) or 0.0),
            'position_market_value': float(getattr(self, '_saved_position_market_value', 0.0) or 0.0),
        }

    def calculate_grids(self, df: pd.DataFrame, window_hours: int = 6):
        """计算3层网格架构: 实体3层 + 虚拟2层 (分5段采样, 去极值取中间3个平均, ATR动态保底)"""
        lookback = window_hours * 60  # 6小时=360, 4小时=240
        if len(df) < lookback:
            lookback = len(df)

        recent = df.tail(lookback)
        if recent.empty:
            return

        # --- 分 5 段截取每段最高最低点 ---
        seg_size = len(recent) // 5
        highs = []
        lows = []

        for i in range(5):
            # 确保最后一段包含所有剩余数据
            start_idx = i * seg_size
            end_idx = (i + 1) * seg_size if i < 4 else len(recent)
            segment = recent.iloc[start_idx:end_idx]

            if not segment.empty:
                highs.append(segment['high'].max())
                lows.append(segment['low'].min())

        # --- 5高5低去极值：排序后去掉最大最小各1个，取中间3个平均 ---
        if len(highs) >= 5:
            sorted_highs = sorted(highs)
            self.grid_top = np.mean(sorted_highs[1:-1])  # 去掉最高和最低，取中间3个
        elif len(highs) >= 1:
            self.grid_top = np.mean(highs)
        else:
            self.grid_top = recent['high'].max()

        if len(lows) >= 5:
            sorted_lows = sorted(lows)
            self.grid_bottom = np.mean(sorted_lows[1:-1])  # 去掉最高和最低，取中间3个
        elif len(lows) >= 1:
            self.grid_bottom = np.mean(lows)
        else:
            self.grid_bottom = recent['low'].min()

        logger.info(f"去极值后 | highs: {[round(h,2) for h in sorted(highs)]} → top: {self.grid_top:.2f} | lows: {[round(l,2) for l in sorted(lows)]} → bottom: {self.grid_bottom:.2f}")

        # --- ATR动态保底宽度 (1小时ATR×2.5, 绝对下限20) ---
        atr = self.calculate_atr(df, period=60)
        atr_min_width = atr * 2.5
        absolute_floor = 20.0
        min_width = max(atr_min_width, absolute_floor)

        raw_width = self.grid_top - self.grid_bottom
        mid = (self.grid_top + self.grid_bottom) / 2.0

        if raw_width < min_width:
            self.grid_bottom = mid - min_width / 2.0
            self.grid_top = mid + min_width / 2.0
            logger.info(f"ATR动态保底扩展 | ATR: {atr:.2f} | ATR×2.5: {atr_min_width:.2f} | 绝对下限: {absolute_floor} | 原宽: {raw_width:.2f} → 扩展到: {min_width:.2f}")
        else:
            logger.info(f"网格宽度正常 | 宽度: {raw_width:.2f} | ATR保底: {min_width:.2f} | 无需扩展")

        # --- 五层空间布局 (3实体 + 2虚拟) ---
        step = (self.grid_top - self.grid_bottom) / 3
        self.entity_grids = [
            self.grid_bottom,              # P0
            self.grid_bottom + step,       # P1
            self.grid_bottom + 2 * step,   # P2
            self.grid_top                  # P3
        ]

        # 虚拟缓冲层
        self.virtual_grids = [
            self.grid_bottom - step,       # P_low_virtual
            self.grid_top + step           # P_high_virtual
        ]

        self.last_grid_calc_time = df.index[-1]

        logger.info(f"网格计算完成 | 窗口: {window_hours}h | 区间: [{self.grid_bottom:.2f} - {self.grid_top:.2f}] | 步长: {step:.2f}")
        logger.info(f"实体层: {[round(x, 2) for x in self.entity_grids]} | 虚拟层: {[round(x, 2) for x in self.virtual_grids]}")

        # 持久化保存
        self._save_grid_state()

    def calculate_rsi(self, prices: List[float], period: int = 14) -> float:
        """计算标准 Wilder's RSI (平滑移动平均)"""
        if len(prices) < period + 1:
            return 50.0

        # 使用 pandas 计算以获得稳定的平滑效果
        s = pd.Series(prices)
        delta = s.diff()

        ups = delta.clip(lower=0)
        downs = -1 * delta.clip(upper=0)

        # 指数平滑移动平均 (Wilder's 方法)
        # alpha = 1 / period
        ma_up = ups.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
        ma_down = downs.ewm(alpha=1/period, min_periods=period, adjust=False).mean()

        rs = ma_up / ma_down
        rsi = 100 - (100 / (1 + rs))

        val = rsi.iloc[-1]
        return float(val) if not np.isnan(val) else 50.0

    def calculate_atr(self, df: pd.DataFrame, period: int = 14) -> float:
        """计算ATR"""
        if len(df) < 2:
            return df['close'].iloc[-1] * 0.02

        recent = df.tail(min(period, len(df)))
        tr_list = []

        for i in range(1, len(recent)):
            high = recent['high'].iloc[i]
            low = recent['low'].iloc[i]
            prev_close = recent['close'].iloc[i-1]

            tr1 = high - low
            tr2 = abs(high - prev_close)
            tr3 = abs(low - prev_close)
            tr_list.append(max(tr1, tr2, tr3))

        return np.mean(tr_list) if tr_list else recent['close'].iloc[-1] * 0.02

    def calculate_dynamic_leverage(self, atr: float, price: float) -> float:
        """动态杠杆: 1x-3x"""
        atr_pct = atr / price * 100

        if atr_pct < 1.5:
            lev = 3.0
        elif atr_pct < 3.0:
            lev = 2.0
        else:
            lev = 1.0

        if lev != self.current_leverage:
            logger.info(f"杠杆调整: {self.current_leverage}x -> {lev}x (ATR: {atr_pct:.2f}%)")
            self.current_leverage = lev
            self.api.set_leverage(lev)

        return lev

    # ====== 黑天鹅/单边行情保护机制 ======
    def _check_blackswan(self, current_price: float, current_time: datetime,
                         context: StrategyContext) -> List[Signal]:
        """检查单边行情保护，返回保护/熔断级别的强制平仓信号。
        
        基于滚动4小时窗口最高/最低价计算偏移度：
        - 🟡 预警(≥3%): 禁开逆势仓，暂停无网格交易
        - 🟠 保护(≥5%): 逆势仓减半，暂停网格重置
        - 🔴 熔断(≥8%): 逆势仓全平，暂停所有交易
        """
        signals = []
        
        # 需要足够数据 (240分钟=4小时)
        if len(self.price_history) < 60:
            return signals
        
        # 取最近240个点(4小时) 或全部可用数据
        window = self.price_history[-240:]
        high_4h = max(window)
        low_4h = min(window)
        
        if high_4h <= 0 or low_4h <= 0:
            return signals
        
        # 计算跌幅和涨幅
        drop_pct = (high_4h - current_price) / high_4h * 100.0  # 正值=下跌
        rise_pct = (current_price - low_4h) / low_4h * 100.0    # 正值=上涨
        
        # 取较大偏移方向
        if drop_pct >= rise_pct:
            deviation = drop_pct
            direction = -1  # 下跌行情
        else:
            deviation = rise_pct
            direction = 1   # 上涨行情
        
        warn_thr, protect_thr, meltdown_thr = self.blackswan_thresholds
        
        # 判定级别
        old_level = self.blackswan_level
        if deviation >= meltdown_thr:
            new_level = 3
        elif deviation >= protect_thr:
            new_level = 2
        elif deviation >= warn_thr:
            new_level = 1
        else:
            new_level = 0
        
        # 恢复逻辑
        if old_level > 0 and new_level == 0:
            if old_level == 1 and deviation < self.blackswan_recovery:
                # 预警解除
                self.blackswan_level = 0
                self.blackswan_direction = 0
                self.blackswan_halved = False
                logger.info(f"[黑天鹅] ✅预警解除 | 偏移回落: {deviation:.1f}%")
                return signals
            elif old_level >= 2:
                # 保护/熔断级别不在此自动解除，由4h重置或0点恢复
                new_level = old_level  # 维持当前级别
        
        # 更新状态
        self.blackswan_level = new_level
        self.blackswan_direction = direction if new_level > 0 else 0
        
        # 级别变化时打日志
        level_names = {0: '正常', 1: '🟡预警', 2: '🟠保护', 3: '🔴熔断'}
        time_now = time.time()
        if new_level != old_level or (new_level > 0 and time_now - self.blackswan_last_log_time >= 300):
            self.blackswan_last_log_time = time_now
            dir_text = '下跌' if direction == -1 else '上涨'
            logger.info(f"[黑天鹅] {level_names[new_level]}触发 | 4h{dir_text}: {deviation:.1f}% | 4h高: {high_4h:.2f} | 4h低: {low_4h:.2f} | 现价: {current_price:.2f}")
        
        if new_level == 0:
            return signals
        
        # ---- 保护级(2): 逆势仓减半 ----
        if new_level >= 2 and not self.blackswan_halved:
            for symbol, pos in context.positions.items():
                if pos.symbol != self.symbol or pos.size == 0:
                    continue
                is_long = pos.size > 0
                # 逆势判断: 下跌行情中持多=逆势, 上涨行情中持空=逆势
                is_adverse = (direction == -1 and is_long) or (direction == 1 and not is_long)
                if is_adverse:
                    if new_level >= 3:
                        # 熔断: 全平
                        close_size = abs(pos.size)
                        side = Side.SELL if is_long else Side.BUY
                        signals.append(Signal(
                            symbol=symbol, side=side, size=close_size,
                            price=current_price, timestamp=current_time,
                            meta={'reason': 'blackswan_meltdown', 'posSide': 'long' if is_long else 'short'}
                        ))
                        logger.info(f"[黑天鹅] 🔴熔断全平 | {'多' if is_long else '空'}仓 {close_size:.4f} ETH | 偏移: {deviation:.1f}%")
                    else:
                        # 保护: 减半
                        close_size = abs(pos.size) / 2.0
                        side = Side.SELL if is_long else Side.BUY
                        signals.append(Signal(
                            symbol=symbol, side=side, size=close_size,
                            price=current_price, timestamp=current_time,
                            meta={'reason': 'blackswan_protect_half', 'posSide': 'long' if is_long else 'short'}
                        ))
                        logger.info(f"[黑天鹅] 🟠保护减仓 | {'多' if is_long else '空'}仓减半: {abs(pos.size):.4f} → {abs(pos.size)-close_size:.4f} ETH | 偏移: {deviation:.1f}%")
            self.blackswan_halved = True
        
        return signals

    def _is_blackswan_blocked(self, side_is_buy: bool) -> bool:
        """检查当前方向是否被黑天鹅保护拦截。
        
        预警级: 禁开逆势仓 (下跌→禁买多, 上涨→禁开空)
        保护级以上: 同上 + 由 _check_blackswan 直接减仓/全平
        熔断级: 禁止所有新开仓
        """
        if self.blackswan_level == 0:
            return False
        
        if self.blackswan_level >= 3:
            # 熔断: 禁止所有新开仓
            return True
        
        # 预警/保护: 只禁逆势方向
        # 下跌行情(direction=-1): 禁买多(side_is_buy=True)
        # 上涨行情(direction=1): 禁开空(side_is_buy=False)
        if self.blackswan_direction == -1 and side_is_buy:
            return True
        if self.blackswan_direction == 1 and not side_is_buy:
            return True
        
        return False

    def _blackswan_blocks_reset(self) -> bool:
        """保护级以上时暂停网格重置"""
        return self.blackswan_level >= 2

    def lstm_confidence(self, price_history: List[float]) -> float:
        """LSTM置信度: -1.0(极强做空) ~ 0.0(中性) ~ +1.0(极强做多)
        替代原lstm_trend，用置信度替代二元判断，保留三层档位(高/中/低置信)"""
        if len(price_history) < 60:
            return 0.0

        recent = price_history[-60:]
        returns = np.diff(recent) / recent[:-1]
        momentum = np.mean(returns) * 100
        volatility = np.std(returns) * 100

        if volatility < 0.001:
            return 0.0

        raw = momentum / (volatility * 2.0)
        confidence = max(-1.0, min(1.0, raw))
        return confidence

    def lstm_trend(self, price_history: List[float]) -> int:
        """轻量LSTM趋势判断(兼容旧接口): -1=做空, 0=中性, 1=做多"""
        confidence = self.lstm_confidence(price_history)
        if confidence >= 0.55:
            return 1
        elif confidence <= -0.55:
            return -1
        return 0

    def check_reset_conditions(self, current_price: float, current_time: datetime) -> Tuple[bool, int]:
        """检查重置条件 (返回: 是否重置, 采样窗口)
        逻辑：
        1. 北京时间每日 00:00 恢复当日 2 次突破重置配额，不触发数据重扫。
        2. 北京时间每 4 小时整点只做一次检查：
           - 价格仍在实体层内 -> 不重置
           - 价格超出实体层但仍在虚拟层内 -> 触发温和重置，使用 4h 窗口
        3. 价格突破虚拟层 (VH/VL) 后开启 2 小时观察期。
        4. 2 小时后仍未回归且配额充足 -> 触发一次突破重置，并使用 2h 窗口重算网格。
        """
        # --- 1. 北京时间 00:00 恢复配额 ---
        cst_time = current_time + timedelta(hours=8)
        current_day = cst_time.date()

        if self.last_reset_day != current_day:
            self.last_reset_day = current_day
            self.daily_reset_count = 0  # 恢复 2 次突破重置机会
            self.breakout_triggered = False
            if self.blackswan_level > 0:
                logger.info(f"[黑天鹅] ✅0点跨天恢复 | 级别: {self.blackswan_level} → 0")
                self.blackswan_level = 0
                self.blackswan_direction = 0
                self.blackswan_halved = False
            logger.info(f"北京时间跨天: {current_day} | 重置配额已恢复 (2次)")

        # --- 2. 4小时温和重置检查 (北京时间 0, 4, 8, 12, 16, 20 点) ---
        current_hour = cst_time.hour
        if current_hour % 4 == 0 and self.last_periodic_reset_hour != current_hour:
            self.last_periodic_reset_hour = current_hour
            if len(self.df_history) >= 240 and self.entity_grids and len(self.entity_grids) >= 4 and len(self.virtual_grids) >= 2:
                entity_low = self.entity_grids[0]
                entity_high = self.entity_grids[-1]
                virtual_low = self.virtual_grids[0]
                virtual_high = self.virtual_grids[1]

                in_entity = entity_low <= current_price <= entity_high
                in_virtual = virtual_low <= current_price <= virtual_high

                if in_entity:
                    logger.info(f"到达 4 小时检查点 (北京时间 {current_hour}点) | 价格仍在实体层内，不重置")
                elif in_virtual:
                    logger.info(f"到达 4 小时检查点 (北京时间 {current_hour}点) | 价格脱离实体层但仍在虚拟层内，触发温和重置")
                    if self.blackswan_level >= 2:
                        old_lvl = self.blackswan_level
                        self.blackswan_level = 0
                        self.blackswan_direction = 0
                        self.blackswan_halved = False
                        logger.info(f"[黑天鹅] ✅4h温和重置恢复 | 级别: {old_lvl} → 0")
                    return True, 4
                else:
                    logger.info(f"到达 4 小时检查点 (北京时间 {current_hour}点) | 价格已在虚拟层外，交由突破观察单独处理")
            else:
                logger.info(f"到达 4 小时检查点，但数据量不足或网格未就绪 ({len(self.df_history)}/240)，跳过温和重置")

        # --- 3. 突破重置检查 (维持每日 2 次) ---
        if self.daily_reset_count >= 2:
            return False, 6

        if len(self.virtual_grids) >= 2:
            lower_bound = self.virtual_grids[0]
            upper_bound = self.virtual_grids[1]
            is_outside = current_price < lower_bound or current_price > upper_bound

            if not self.breakout_triggered:
                if is_outside:
                    self.breakout_triggered = True
                    self.breakout_time = current_time
                    logger.info(f"警告：价格突破虚拟层 | 价格: {current_price:.2f} | 观察期开始 (2h)")
            else:
                if not is_outside:
                    self.breakout_triggered = False
                    logger.info(f"价格回归虚拟层内 | 价格: {current_price:.2f} | 观察期取消")
                else:
                    elapsed = (current_time - self.breakout_time).total_seconds()
                    if elapsed >= 2 * 3600:
                        self.breakout_triggered = False
                        self.daily_reset_count += 1
                        self.breakout_reset_count += 1
                        logger.info(f"观察期满 2 小时未回归 | 触发突破重置 | 第 {self.daily_reset_count} 次")
                        return True, 2  # 突破重置使用最近 2 小时窗口

        return False, 6

    def execute_trade(self, side: str, size: float, price: float, reason: str):
        """执行交易"""
        try:
            actual_size = size * self.current_leverage
            result = self.api.place_order(side=side, sz=actual_size)

            if result.get('code') == '0':
                self.trade_count += 1
                logger.info(f"交易执行 | {side} | 数量: {actual_size:.4f} | 价格: {price:.2f} | 原因: {reason}")
                return True
            else:
                logger.error(f"交易失败 | {result.get('msg')}")
                return False

        except Exception as e:
            logger.error(f"交易异常: {e}")
            return False

    def close_all_positions(self, current_price: float):
        """平仓所有持仓"""
        try:
            result = self.api.close_position()
            if result.get('code') == '0':
                logger.info("全部持仓已平仓")
                self.positions.clear()
                return True
            else:
                logger.error(f"平仓失败: {result.get('msg')}")
                return False
        except Exception as e:
            logger.error(f"平仓异常: {e}")
            return False

    def get_current_layer(self, price: float) -> Optional[int]:
        """确定当前价格所在层 (5层空间架构: 3实体 + 2虚拟)"""
        if len(self.entity_grids) < 4:
            return None

        # 虚拟层下界检查
        if price < self.virtual_grids[0]:
            return None

        # 层级映射:
        # -1: [VirtualLow, P0)
        #  0: [P0, P1) - 底层实体
        #  1: [P1, P2) - 中层实体
        #  2: [P2, P3) - 高层实体
        #  3: [P3, VirtualHigh)

        if price < self.entity_grids[0]:
            return -1
        elif price < self.entity_grids[1]:
            return 0
        elif price < self.entity_grids[2]:
            return 1
        elif price < self.entity_grids[3]:
            return 2
        elif price < self.virtual_grids[1]:
            return 3
        else:
            return None
    # 策略现在仅通过 on_data 和 on_fill 与外部引擎交互
