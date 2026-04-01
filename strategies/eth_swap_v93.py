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
import hmac
import hashlib
import base64
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple, Optional, Any
import logging
from logging.handlers import TimedRotatingFileHandler
import sys

# 导入系统组件
from .base import BaseStrategy
from core.types import Signal, MarketData, StrategyContext, Side, OrderType
from dashboard.server import create_dashboard

# 配置日志
LOG_DIR = 'logs'
if not os.path.exists(LOG_DIR): os.makedirs(LOG_DIR)
DATA_DIR = 'data'
if not os.path.exists(DATA_DIR): os.makedirs(DATA_DIR)

# 配置文件处理器 (每天午夜切割，保留30天当天及历史日志)
file_handler = TimedRotatingFileHandler(
    filename=os.path.join(LOG_DIR, 'v93_innovation_5100.log'),
    when='MIDNIGHT',
    interval=1,
    backupCount=30,
    encoding='utf-8'
)
file_handler.setLevel(logging.INFO)
file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler.setFormatter(file_formatter)

# 配置控制台处理器 (同步显示诊断与心跳信息)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(file_formatter) # 使用更详细的格式器，保持与文件一致
# console_handler.setFormatter(console_formatter) # 原简洁格式暂存备份

# 重新组装根 Logger
logger = logging.getLogger('V9.3-Innovation')
logger.setLevel(logging.INFO)

# 清理可能存在的旧 handlers 避免重复打印
if logger.hasHandlers():
    logger.handlers.clear()

logger.addHandler(file_handler)
logger.addHandler(console_handler)

class OKXAPI:
    """OKX API 封装"""

    def __init__(self, config: Dict):
        self.api_key = config['api_key']
        self.api_secret = config['api_secret']
        self.passphrase = config['passphrase']
        self.base_url = "https://www.okx.com"
        self.testnet = config.get('testnet', True)

        if self.testnet:
            self.base_url = "https://www.okx.com"

        self.symbol = config.get('symbol', 'ETH-USDT-SWAP')

    def _sign(self, timestamp: str, method: str, request_path: str, body: str = "") -> str:
        """生成签名"""
        message = timestamp + method.upper() + request_path + body
        mac = hmac.new(
            self.api_secret.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256
        )
        return base64.b64encode(mac.digest()).decode('utf-8')

    def _request(self, method: str, path: str, body: Dict = None) -> Dict:
        """发送请求并带有重试机制"""
        max_retries = 3
        for i in range(max_retries):
            timestamp = datetime.utcnow().isoformat(timespec='milliseconds') + 'Z'
            body_json = json.dumps(body) if body else ""

            headers = {
                'OK-ACCESS-KEY': self.api_key,
                'OK-ACCESS-SIGN': self._sign(timestamp, method, path, body_json),
                'OK-ACCESS-TIMESTAMP': timestamp,
                'OK-ACCESS-PASSPHRASE': self.passphrase,
                'Content-Type': 'application/json'
            }

            if self.testnet:
                headers['x-simulated-trading'] = '1'

            url = self.base_url + path
            try:
                if method.upper() == 'GET':
                    response = requests.get(url, headers=headers, timeout=10)
                else:
                    response = requests.post(url, headers=headers, data=body_json, timeout=10)

                if response.status_code == 200:
                    return response.json()
                else:
                    logger.warning(f"API请求状态码异常 ({response.status_code}): {response.text} | 重试 {i+1}/{max_retries}")
                    time.sleep(1)
            except Exception as e:
                logger.error(f"API请求异常: {e} | 重试 {i+1}/{max_retries}")
                time.sleep(1)

        return {'code': '-1', 'msg': 'Max retries exceeded'}

    def get_ticker(self) -> Dict:
        """获取最新价格"""
        path = f"/api/v5/market/ticker?instId={self.symbol}"
        return self._request('GET', path)

    def get_candles(self, bar: str = "1m", limit: int = 100) -> pd.DataFrame:
        """获取K线数据, 支持超过100个的分页查询"""
        all_data = []
        last_ts = ""

        # 核心逻辑：分批获取，通过 history-candles 翻页
        remaining = limit
        while remaining > 0:
            current_limit = min(remaining, 100)
            if not last_ts:
                # 第一次：获取最新数据
                path = f"/api/v5/market/candles?instId={self.symbol}&bar={bar}&limit={current_limit}"
            else:
                # 后续：获取旧数据
                path = f"/api/v5/market/history-candles?instId={self.symbol}&bar={bar}&after={last_ts}&limit={current_limit}"

            data = self._request('GET', path)
            if data.get('code') == '0' and data.get('data'):
                batch = data['data']
                all_data.extend(batch)
                if len(batch) < current_limit:
                    break
                last_ts = batch[-1][0] # 最后一个点的时间戳用于翻页
                remaining -= len(batch)
            else:
                break

        if all_data:
            df = pd.DataFrame(all_data, columns=[
                'ts', 'open', 'high', 'low', 'close', 'vol', 'volCcy', 'volCcyQuote', 'confirm'
            ])
            df['ts'] = pd.to_datetime(df['ts'].astype(float), unit='ms', utc=True)
            df.set_index('ts', inplace=True)
            df = df.astype(float)
            # OKX 返回的是从新到旧，需要排序
            return df.sort_index()

        return pd.DataFrame()

    def get_position(self) -> Dict:
        """获取持仓"""
        path = f"/api/v5/account/positions?instId={self.symbol}"
        return self._request('GET', path)

    def place_order(self, side: str, sz: float, px: float = None,
                   ord_type: str = "market", td_mode: str = "cross") -> Dict:
        """下单"""
        body = {
            "instId": self.symbol,
            "tdMode": td_mode,
            "side": side,
            "ordType": ord_type,
            "sz": str(sz)
        }
        if px and ord_type == "limit":
            body["px"] = str(px)

        return self._request('POST', '/api/v5/trade/order', body)

    def close_position(self, pos_side: str = None) -> Dict:
        """平仓"""
        body = {
            "instId": self.symbol,
            "mgnMode": "cross",
            "autoCxl": True
        }
        if pos_side:
            body["posSide"] = pos_side
        return self._request('POST', '/api/v5/trade/close-position', body)

    def set_leverage(self, lever: float, mgn_mode: str = "cross") -> Dict:
        """设置杠杆"""
        body = {
            "instId": self.symbol,
            "lever": str(lever),
            "mgnMode": mgn_mode
        }
        return self._request('POST', '/api/v5/account/set-leverage', body)


class V93Strategy(BaseStrategy):
    """V9.3-Innovation 策略核心 - 3层网格+动态RSI+网格归并"""

    def __init__(self, config: Dict = None, **kwargs):
        # 统一配置处理
        self.config = config or kwargs
        super().__init__(name="V9.3-Innovation-v1.1", **self.config)

        self.name = "V9.5-Innovation" # v9.5：按层加仓 + 远层优先平仓 + 禁止对冲
        self.symbol = self.config.get('symbol', 'ETH-USDT-SWAP')
        self.last_run_time = None

        # 处理 OKX API (仅在 Standalone 模式下需要，即 config 中包含 API 密钥时)
        self.api = None
        okx_config = self.config.get('okx', self.config)
        if isinstance(okx_config, dict) and 'api_key' in okx_config:
            self.api = OKXAPI(okx_config)
            logger.info("API 模块初始化成功 (Standalone 模式)")
        else:
            logger.info("API 模块未初始化 (Engine 模式)")

        # 初始化 Dashboard Server (仅在 Standalone 模式且未禁用时)
        self.server = None
        if self.config.get('standalone', False) or (self.api is not None and self.config.get('port')):
            port = self.config.get('port', 5090)
            self.server = create_dashboard(port=port)
            self.server.version = "v9.5"
            logger.info(f"Dashboard Server 初始化成功 | 端口: {port}")
        else:
            logger.info("Dashboard Server 跳过初始化 (由外部引擎管理)")

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
        self.occupied_long_layers = set()  # 追踪已开单的长仓层级
        self.occupied_short_layers = set() # 追踪已开单的短仓层级

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

    def on_start(self):
        """引擎启动时调用"""
        logger.info(f"策略 {self.name} 已启动")

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
        trend = self.lstm_trend(self.price_history)

        # 3. 动态RSI阈值 (基于趋势强度)
        self._update_dynamic_rsi_thresholds(atr, current_price, trend)

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

            # 网格重置后，对齐已有仓位到新网格层边界
            # 规则: 多仓→所在层下边界, 空仓→所在层上边界
            # 网格外: 多仓→P0, 空仓→P3
            if context.positions and self.entity_grids:
                grids = self.entity_grids  # [P0, P1, P2, P3]
                for pos in context.positions.values():
                    if pos.symbol == self.symbol and abs(pos.size) > 1e-8:
                        old_price = pos.avg_price
                        is_long = pos.size > 0
                        # 找入场价落在哪一层
                        if old_price < grids[0]:
                            # 低于P0: 多仓→P0, 空仓→P0(已在最低)
                            new_price = grids[0]
                        elif old_price >= grids[-1]:
                            # 高于P3: 空仓→P3, 多仓→P3(已在最高)
                            new_price = grids[-1]
                        else:
                            # 在网格内, 找所在层
                            for i in range(len(grids) - 1):
                                if grids[i] <= old_price < grids[i + 1]:
                                    new_price = grids[i] if is_long else grids[i + 1]
                                    break
                        pos.avg_price = new_price
                        logger.info(f"仓位对齐 | {'多' if is_long else '空'}仓 | "
                                   f"旧入场价: {old_price:.2f} -> 新入场价: {new_price:.2f} | "
                                   f"层边界: {[round(g,2) for g in grids]}")

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
                 self._print_enhanced_logs(rsi, current_price, trend, layer, context)
                 return signals
             
             self._print_enhanced_logs(rsi, current_price, trend, layer, context)
             return []


        # 7. 获取持仓状态 (从 context) 并同步层级占用状态
        has_long = False
        has_short = False
        for pos in context.positions.values():
            if pos.symbol == self.symbol:
                if pos.size > 0: has_long = True
                if pos.size < 0: has_short = True
        
        # 自动同步：如果持仓消失，则清空对应的层级占用状态
        if not has_long:
            if self.occupied_long_layers:
                logger.info(f"[同步] 多仓已清空，释放已占用的层级: {list(self.occupied_long_layers)}")
                self.occupied_long_layers.clear()
        if not has_short:
            if self.occupied_short_layers:
                logger.info(f"[同步] 空仓已清空，释放已占用的层级: {list(self.occupied_short_layers)}")
                self.occupied_short_layers.clear()

        # 8. 核心交易逻辑：出场优先 + 区域入场
        # 8.1 统一出场逻辑 (已禁用: 2026-04-01, 与网格层平仓冲突)
        # exit_signals = self._check_exit_signals(rsi, current_price, current_time, layer, context)
        # signals.extend(exit_signals)

        # 8.2 熔断级检查 (仅限制新开仓)
        if self.blackswan_level >= 3:
            if signals:
                self.last_trade_time = time_now
            self._print_enhanced_logs(rsi, current_price, trend, layer, context)
            return signals

        # 8.3 入场逻辑 (如果已经触发平项，则跳过本次循环的入场检查，避免同根K线反复摩擦)
        if not signals:
            if layer is None:
                # 预警级以上暂停无网格入场
                if self.blackswan_level < 1:
                    entry_signals = self._no_grid_trading(rsi, has_long, has_short, current_price, current_time, trend, context)
                    signals.extend(entry_signals)
            else:
                # 正常网格交易 (包含实体层与虚拟层)
                entry_signals = self._grid_trading(layer, rsi, has_long, has_short, current_price, current_time, trend, context)
                signals.extend(entry_signals)

        # 更新状态并记录冷却时间（如果有信号）
        if signals:
            self.last_trade_time = time_now

        self._print_enhanced_logs(rsi, current_price, trend, layer, context)
        return signals

    def _print_enhanced_logs(self, rsi, current_price, trend, layer, context: StrategyContext):
        """打印用户要求的增强型诊断与心跳日志 (每分钟一次)"""
        # 1. 检查是否到达 60 秒间隔
        if not hasattr(self, '_last_heartbeat_time') or (time.time() - self._last_heartbeat_time >= 60):
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00:00")
            grid_desc = f"[{self.grid_bottom:.1f}, {self.grid_top:.1f}]" if self.grid_top > 0 else "[未初始化]"
            rsi_limit = f"(限 {self.rsi_oversold:.0f}/{self.rsi_overbought:.0f})"

            # 1. 策略诊断 (合并到分钟级别)
            diag_log = f"[策略诊断] {now_str} | 价格: {current_price:.2f} | RSI: {rsi:.1f} {rsi_limit} | 网格: {grid_desc}"
            logger.info(diag_log)

            # 2. 引擎心跳
            # 计算收益率
            equity = context.meta.get('total_equity', context.total_value)
            initial = context.meta.get('initial_equity', equity)
            profit_pct = ((equity / initial) - 1) * 100 if initial > 0 else 0.0
            
            # 计算持仓量
            pos_size = 0.0
            for pos in context.positions.values():
                if pos.symbol == self.symbol:
                    pos_size = abs(pos.size)
            
            pos_desc = f"{pos_size:.3f} ETH"
            heartbeat_log = (f"[引擎心跳] {datetime.now().strftime('%H:%M:%S')} | 价格: {current_price:.2f} | "
                           f"持仓: {pos_desc} | RSI: {rsi:.1f} | 网格: {grid_desc} | 收益: {profit_pct:+.2f}%")
            logger.info(heartbeat_log)
            
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
            'current_price': current_price
        }

    def _update_dynamic_rsi_thresholds(self, atr: float, price: float, trend: int):
        """动态RSI阈值：方案A

        只放宽实体层一档，虚拟层保持原严度不变。
        弱趋势: 实体 35/45/65/75，虚拟 30/80
        强趋势: 实体 40/50/60/70，虚拟 35/75
        """
        if abs(trend) >= 1:  # 强趋势
            # 实体层：放宽一档
            self.rsi_oversold = 40.0
            self.rsi_overbought = 70.0
            self.rsi_exit_oversold = 50.0
            self.rsi_exit_overbought = 60.0
            # 虚拟层：保持原严度
            self.virtual_rsi_oversold = 35.0
            self.virtual_rsi_overbought = 75.0
        else:  # 弱趋势/震荡
            # 实体层：放宽一档
            self.rsi_oversold = 35.0
            self.rsi_overbought = 75.0
            self.rsi_exit_oversold = 45.0
            self.rsi_exit_overbought = 65.0
            # 虚拟层：保持原严度
            self.virtual_rsi_oversold = 30.0
            self.virtual_rsi_overbought = 80.0

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
                         context: StrategyContext) -> List[Signal]:
        """无网格模式：两小时观察期内交易，仅一次，条件写死

        触发条件（与原逻辑一致）：
        - 弱趋势：做多 RSI <= 28，做空 RSI >= 80
        - 强趋势：做多 RSI <= 25，做空 RSI >= 75
        - 两小时观察期内仅触发一次（买入或卖出）
        - 观察期结束计数归零
        """
        signals = []

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

        # 下半部做多：跌破虚拟低层 + RSI阈值 + 该区域未开仓
        # 备注：无网格区域我们定义为一个特殊的层级索引 -2
        if current_price < virtual_low and rsi <= long_threshold and -2 not in self.occupied_long_layers and not has_short and not _position_in_layer(long_positions, -2):
            signals.append(Signal(
                symbol=self.symbol, side=Side.BUY, size=no_grid_size,
                price=current_price, timestamp=current_time,
                meta={'reason': 'no_grid_long_entry', 'posSide': 'long', 'level': -2}
            ))
            self.occupied_long_layers.add(-2)
            self._no_grid_triggered_in_breakout = True
            self.last_entry_time = time.time()
            logger.info(f"无网格交易 | 下半部做多 | {trend_name} | RSI: {rsi:.1f} <= {long_threshold:.0f} | 价格: {current_price:.2f} | 观察期剩余: {((2*3600-elapsed)/60):.0f}分钟")

        # 上半部做空：涨破虚拟高层 + RSI阈值 + 该区域未开仓
        # 备注：无网格区域我们定义为一个特殊的层级索引 4
        elif current_price > virtual_high and rsi >= short_threshold and 4 not in self.occupied_short_layers and not has_long and not _position_in_layer(short_positions, 4):
            signals.append(Signal(
                symbol=self.symbol, side=Side.SELL, size=no_grid_size,
                price=current_price, timestamp=current_time,
                meta={'reason': 'no_grid_short_entry', 'posSide': 'short', 'level': 4}
            ))
            self.occupied_short_layers.add(4)
            self._no_grid_triggered_in_breakout = True
            self.last_entry_time = time.time()
            logger.info(f"无网格交易 | 上半部做空 | {trend_name} | RSI: {rsi:.1f} >= {short_threshold:.0f} | 价格: {current_price:.2f} | 观察期剩余: {((2*3600-elapsed)/60):.0f}分钟")

        else:
            if should_log:
                logger.info(f"无网格观察 | 条件未满足 | {trend_name} | RSI: {rsi:.1f} | 做多阈值: {long_threshold:.0f} | 做空阈值: {short_threshold:.0f}")

        return signals

    def _grid_trading(self, layer: int, rsi: float, has_long: bool, has_short: bool,
                      current_price: float, current_time: datetime, trend: int,
                      context: StrategyContext) -> List[Signal]:
        """按层级拦截逻辑：同层拦截，异层放行"""
        signals = []

        # ========== 平仓逻辑（v9.5 新增：平最远层） ==========
        # 从 _check_exit_signals 提取，按层判断 + 只平离当前层最远的持仓
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
                key=lambda pos: abs(
                    (_position_level(pos) if _position_level(pos) is not None else (current_layer or 0))
                    - (current_layer or 0)
                )
            )

        def _position_in_layer(positions, target_layer):
            """检查是否存在同向持仓已在目标层级，返回该持仓或None"""
            if not positions or target_layer is None:
                return None
            for pos in positions:
                lvl = _position_level(pos)
                if lvl == target_layer:
                    return pos
            return None

        # Layer1 中点：上下半层过滤（平多只在上半层，平空只在下半层）
        layer1_mid = (self.entity_grids[1] + self.entity_grids[2]) / 2 if self.entity_grids and len(self.entity_grids) >= 4 else 0

        # 平多阈值（超买）：layer1最难平(+10)，layer2中间(+5)，layer3最容易(基准)
        exit_ob_layer1 = self.rsi_exit_overbought + 10.0
        exit_ob_layer2 = self.rsi_exit_overbought + 5.0
        exit_ob_layer3 = self.rsi_exit_overbought

        # 平空阈值（超卖）：layer1最难平(-10)，layer0中间(-5)，layer-1最容易(基准)
        exit_os_layer1 = self.rsi_exit_oversold - 10.0
        exit_os_layer0 = self.rsi_exit_oversold - 5.0
        exit_os_layerN1 = self.rsi_exit_oversold

        eff_layer = layer if layer is not None else (3 if current_price > self.grid_top else 0)

        # 平多检查（layer 1/2/3 或网格外高位）
        if long_positions and eff_layer in (1, 2, 3):
            if not (eff_layer == 1 and layer1_mid > 0 and current_price < layer1_mid):  # L1下半层过滤
                rsi_threshold = exit_ob_layer3 if eff_layer == 3 else (exit_ob_layer2 if eff_layer == 2 else exit_ob_layer1)
                if rsi >= rsi_threshold:
                    pos_to_close = _pick_farthest_position(long_positions, eff_layer)
                    if pos_to_close:
                        signals.append(Signal(
                            symbol=self.symbol, side=Side.SELL, size=abs(pos_to_close.size),
                            price=current_price, timestamp=current_time,
                            meta={'reason': f'exit_long_farthest_L{eff_layer}', 'posSide': 'long'}
                        ))
                        logger.info(f"[平仓] 平多(最远层) | RSI: {rsi:.1f} >= {rsi_threshold:.1f} | 平仓: {pos_to_close.size:.4f} ETH | 触发层: L{eff_layer}")

        # 平空检查（layer -1/0/1 或网格外低位）
        if short_positions and eff_layer in (-1, 0, 1):
            if not (eff_layer == 1 and layer1_mid > 0 and current_price > layer1_mid):  # L1上半层过滤
                rsi_threshold = exit_os_layerN1 if eff_layer == -1 else (exit_os_layer0 if eff_layer == 0 else exit_os_layer1)
                if rsi <= rsi_threshold:
                    pos_to_close = _pick_farthest_position(short_positions, eff_layer)
                    if pos_to_close:
                        signals.append(Signal(
                            symbol=self.symbol, side=Side.BUY, size=abs(pos_to_close.size),
                            price=current_price, timestamp=current_time,
                            meta={'reason': f'exit_short_farthest_L{eff_layer}', 'posSide': 'short'}
                        ))
                        logger.info(f"[平仓] 平空(最远层) | RSI: {rsi:.1f} <= {rsi_threshold:.1f} | 平仓: {abs(pos_to_close.size):.4f} ETH | 触发层: L{eff_layer}")
        # ========== 平仓逻辑结束 ==========

        # --- 入场逻辑 ---
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

        # 1. 做多 (在层 -1 / 0 低位区域)
        if layer in (-1, 0):
            if has_short:
                if should_log_intercept:
                    logger.info(f"  [拦截开多] 价格 {current_price:.2f} 在做多区，但持有空仓，跳过对冲")
            elif layer in self.occupied_long_layers or _position_in_layer(long_positions, layer):
                if should_log_intercept:
                    logger.info(f"  [拦截开多] 价格 {current_price:.2f} 在层级 {layer}，但该层已开仓，禁止重复加仓")
            else:
                # 开仓去重：防止短时间内重复开仓
                if time_now - self.last_entry_time < self.min_entry_interval:
                    if should_log_intercept:
                        logger.info(f"  [开仓冷却] 距上次开仓仅 {time_now - self.last_entry_time:.0f}秒，跳过")
                    return signals
                
                # 黑天鹅拦截
                if self._is_blackswan_blocked(side_is_buy=True):
                    if should_log_intercept:
                        logger.info(f"  [黑天鹅拦截] 禁止开多 | 级别: {self.blackswan_level}")
                elif layer == -1:
                    # 虚拟低层
                    if rsi <= virtual_long_threshold:
                        size = base_size_with_lev
                        signals.append(Signal(
                            symbol=self.symbol, side=Side.BUY, size=size,
                            price=current_price, timestamp=current_time,
                            meta={'reason': 'layer-1_long_entry', 'posSide': 'long', 'level': layer}
                        ))
                        self.occupied_long_layers.add(layer)
                        logger.info(f"入场信号 | 做多 (虚拟低层) | RSI: {rsi:.1f} | 价格: {current_price:.2f}")
                        self.last_entry_time = time_now
                else:
                    # 实体低层 (layer 0)
                    size = base_size_with_lev
                    signals.append(Signal(
                        symbol=self.symbol, side=Side.BUY, size=size,
                        price=current_price, timestamp=current_time,
                        meta={'reason': 'layer0_long_entry', 'posSide': 'long', 'level': layer}
                    ))
                    self.occupied_long_layers.add(layer)
                    logger.info(f"入场信号 | 做多 (层0) | 价格: {current_price:.2f}")
                    self.last_entry_time = time_now

        # 2. 做空 (在层 2 / 3 高位区域)
        elif layer in (2, 3):
            if has_long:
                if should_log_intercept:
                    logger.info(f"  [拦截开空] 价格 {current_price:.2f} 在做空区，但持有多仓，跳过对冲")
            elif layer in self.occupied_short_layers or _position_in_layer(short_positions, layer):
                if should_log_intercept:
                    logger.info(f"  [拦截开空] 价格 {current_price:.2f} 在层级 {layer}，但该层已开仓，禁止重复加仓")
            else:
                # 开仓去重
                if time_now - self.last_entry_time < self.min_entry_interval:
                    if should_log_intercept:
                        logger.info(f"  [开仓冷却] 距上次开仓仅 {time_now - self.last_entry_time:.0f}秒，跳过")
                    return signals
                
                if self._is_blackswan_blocked(side_is_buy=False):
                    if should_log_intercept:
                        logger.info(f"  [黑天鹅拦截] 禁止开空 | 级别: {self.blackswan_level}")
                elif layer == 3:
                    # 虚拟高层
                    if rsi >= virtual_short_threshold:
                        size = base_size_with_lev
                        signals.append(Signal(
                            symbol=self.symbol, side=Side.SELL, size=size,
                            price=current_price, timestamp=current_time,
                            meta={'reason': 'layer3_short_entry', 'posSide': 'short', 'level': layer}
                        ))
                        self.occupied_short_layers.add(layer)
                        logger.info(f"入场信号 | 做空 (虚拟高层) | RSI: {rsi:.1f} | 价格: {current_price:.2f}")
                        self.last_entry_time = time_now
                else:
                    # 实体高层 (layer 2)
                    size = base_size_with_lev
                    signals.append(Signal(
                        symbol=self.symbol, side=Side.SELL, size=size,
                        price=current_price, timestamp=current_time,
                        meta={'reason': 'layer2_short_entry', 'posSide': 'short', 'level': layer}
                    ))
                    self.occupied_short_layers.add(layer)
                    logger.info(f"入场信号 | 做空 (层2) | 价格: {current_price:.2f}")
                    self.last_entry_time = time_now

        return signals

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

        # 阈值配置: 挂钩动态RSI基准值，每层递减固定5 [待确认]
        # 平多超买: layer1(基准+10) > layer2(基准+5) > layer3(基准)
        # 平空超卖: layer1(基准-10) < layer0(基准-5) < layer-1(基准)
        exit_ob_layer1 = self.rsi_exit_overbought + 10.0  # layer1: 最难平
        exit_ob_layer2 = self.rsi_exit_overbought + 5.0   # layer2: 中间档
        exit_ob_layer3 = self.rsi_exit_overbought          # layer3: 最容易平

        exit_os_layer1 = self.rsi_exit_oversold - 10.0  # layer1: 最难平
        exit_os_layer0 = self.rsi_exit_oversold - 5.0   # layer0: 中间档
        exit_os_layerN1 = self.rsi_exit_oversold         # layer-1: 最容易平

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
        """返回给 Dashboard 的状态 (适配 V93Innovation)"""
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
                'occupied_short_layers': list(self.occupied_short_layers)
            }

            # 1. 保存最新状态 (覆盖)
            state_file = os.path.join(DATA_DIR, 'v93_state.json')
            with open(state_file, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=4)

            # 2. 保存增量历史 (追加)
            history_file = os.path.join(DATA_DIR, 'v93_grid_history.json')
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

    def _load_grid_state(self):
        """从文件加载网格状态"""
        state_file = os.path.join(DATA_DIR, 'v93_state.json')
        if os.path.exists(state_file):
            try:
                with open(state_file, 'r', encoding='utf-8') as f:
                    state = json.load(f)

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
                self.occupied_long_layers = set(state.get('occupied_long_layers', []))
                self.occupied_short_layers = set(state.get('occupied_short_layers', []))

                if self.entity_grids and self.virtual_grids:
                    logger.info(f"成功从文件恢复网格状态 | 区间: [{self.grid_bottom:.2f} - {self.grid_top:.2f}]")
            except Exception as e:
                logger.error(f"加载网格状态失败: {e}")

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

        # --- ATR动态保底宽度 (1小时ATR×2.5, 绝对下限50) ---
        atr = self.calculate_atr(df, period=60)
        atr_min_width = atr * 2.5
        absolute_floor = 50.0
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

    def lstm_trend(self, price_history: List[float]) -> int:
        """轻量LSTM趋势判断: -1=做空, 0=中性, 1=做多"""
        if len(price_history) < 60:
            return 0

        recent = price_history[-60:]
        returns = np.diff(recent) / recent[:-1]
        momentum = np.mean(returns) * 100
        volatility = np.std(returns) * 100

        if momentum > volatility * 0.5 and momentum > 0.02:
            return 1
        elif momentum < -volatility * 0.5 and momentum < -0.02:
            return -1
        return 0

    def check_reset_conditions(self, current_price: float, current_time: datetime) -> Tuple[bool, int]:
        """检查重置条件 (返回: 是否重置, 采样窗口)
        逻辑：
        1. 不做任何定时强制重置。
        2. 每日 00:00 (北京时间) 恢复当日 2 次重置配额，不触发数据重扫。
        3. 价格突破虚拟层 (VH/VL) 后开启 2 小时观察期。
        4. 2 小时后仍未回归且配额充足 -> 触发一次重置，并使用 4h 窗口重算网格。
        """
        # --- 1. 北京时间 00:00 恢复配额 ---
        # 假设服务器时间/K线时间为 UTC，转换为北京时间 (UTC+8)
        cst_time = current_time + timedelta(hours=8)
        current_day = cst_time.date()

        if self.last_reset_day != current_day:
            self.last_reset_day = current_day
            self.daily_reset_count = 0  # 恢复 2 次机会
            self.breakout_triggered = False
            # 0点恢复黑天鹅保护
            if self.blackswan_level > 0:
                logger.info(f"[黑天鹅] ✅0点跨天恢复 | 级别: {self.blackswan_level} → 0")
                self.blackswan_level = 0
                self.blackswan_direction = 0
                self.blackswan_halved = False
            logger.info(f"北京时间跨天: {current_day} | 重置配额已恢复 (2次)")

        # --- 2. 4小时温和重置检查 (北京时间 0, 4, 8, 12, 16, 20 点) ---
        current_hour = cst_time.hour
        if current_hour % 4 == 0 and self.last_periodic_reset_hour != current_hour:
            if len(self.df_history) >= 350:
                self.last_periodic_reset_hour = current_hour
                logger.info(f"到达 4 小时定期重置点 (北京时间 {current_hour}点) | 触发网格温和重算")
                # 保护/熔断级在4h重置时自动降级恢复
                if self.blackswan_level >= 2:
                    old_lvl = self.blackswan_level
                    self.blackswan_level = 0
                    self.blackswan_direction = 0
                    self.blackswan_halved = False
                    logger.info(f"[黑天鹅] ✅4h重置恢复 | 级别: {old_lvl} → 0")
                return True, 6
            else:
                logger.info(f"到达 4 小时重置时机，但数据量不足 ({len(self.df_history)}/350)，跳过重置")

        # --- 3. 突破重置检查 (维持每日 2 次) ---
        if self.daily_reset_count >= 2:
            return False, 6

        if len(self.virtual_grids) >= 2:
            lower_bound = self.virtual_grids[0]
            upper_bound = self.virtual_grids[1]

            # 检测是否突破
            is_outside = current_price < lower_bound or current_price > upper_bound

            if not self.breakout_triggered:
                if is_outside:
                    self.breakout_triggered = True
                    self.breakout_time = current_time
                    logger.info(f"警告：价格突破虚拟层 | 价格: {current_price:.2f} | 观察期开始 (2h)")
            else:
                # 已在观察期内
                if not is_outside:
                    # 价格回归，取消观察
                    self.breakout_triggered = False
                    logger.info(f"价格回归网格内 | 价格: {current_price:.2f} | 观察期取消")
                else:
                    # 价格持续在外面，检查是否满 2 小时
                    elapsed = (current_time - self.breakout_time).total_seconds()
                    if elapsed >= 2 * 3600:
                        self.breakout_triggered = False
                        self.daily_reset_count += 1
                        self.breakout_reset_count += 1
                        logger.info(f"观察期满 2 小时未回归 | 触发紧急重置 | 第 {self.daily_reset_count} 次")
                        return True, 4  # 触发重置，且使用 4 小时窗口

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

    def run_cycle(self):
        """运行一个交易周期 (Standalone模式，适配 3层实体架构)"""
        try:
            if not self.api:
                logger.warning("API 模块未初始化，无法通过 run_cycle 独立运行。请检查配置或改用 Engine 模式。")
                return

            # 1. 获取数据 (360根预热)
            df = self.api.get_candles(bar="1m", limit=400)
            if df.empty:
                logger.warning("获取K线数据失败")
                return

            # 2. 转换数据为 MarketData
            row = df.iloc[-1]
            data = MarketData(
                symbol=self.symbol,
                timestamp=df.index[-1].to_pydatetime(),
                open=float(row['open']),
                high=float(row['high']),
                low=float(row['low']),
                close=float(row['close']),
                volume=float(row['vol'])
            )

            # 3. 构造 Mock Context (Standalone模式不依赖引擎)
            context = StrategyContext()
            context.cash = 10000.0  # 默认 Mock 资金

            # 获取实际持仓
            pos_info = self.api.get_position()
            if pos_info.get('code') == '0' and pos_info.get('data'):
                for p in pos_info['data']:
                    inst_id = p.get('instId')
                    size = float(p.get('pos', 0))
                    if size != 0:
                        from core.types import Position
                        context.positions[inst_id] = Position(
                            symbol=inst_id,
                            size=size,
                            avg_price=float(p.get('avgPx', 0)),
                            unrealized_pnl=float(p.get('upl', 0))
                        )

            # 4. 调用核心策略逻辑
            signals = self.on_data(data, context)

            # 5. 执行信号
            for sig in signals:
                side = "buy" if sig.side == Side.BUY else "sell"
                # Standalone 模式直接在这里通过 API 下单
                self.execute_trade(side, sig.size, sig.price, sig.meta.get('reason', 'on_data_signal'))

            # 6. 处理杠杆调整请求
            if 'requested_leverage' in context.meta:
                lev = context.meta['requested_leverage']
                self.api.set_leverage(lev)

        except Exception as e:
            logger.error(f"周期运行异常: {e}")
            import traceback
            logger.error(traceback.format_exc())

    def run(self):
        """主循环 (Standalone模式)"""
        logger.info("="*60)
        logger.info(f"策略 {self.name} 启动")
        logger.info(f"端口: {self.config.get('port', 5090)}")
        logger.info(f"标的: {self.symbol}")
        logger.info(f"特色: 3层实体 + 2层虚拟 (v1.1)")
        logger.info("="*60)

        # 启动看板 (如果存在)
        if self.server:
            self.server.start_background()
        else:
            logger.info("看板未初始化或由外部管理")

        while True:
            try:
                self.run_cycle()

                # 同步状态到看板
                if self.server:
                    status = self.get_status()
                    # 补充一些全局信息
                    status.update({
                        'symbol': self.symbol,
                        'total_value': 0, # TODO: 获取账户余额
                        'cash': 0,
                        'position_value': 0,
                        'pnl_pct': 0,
                        'history_candles': self.df_history.reset_index().rename(columns={'ts': 'time'}).to_dict('records') if not self.df_history.empty else []
                    })
                    self.server.update(status)

                time.sleep(60)

            except KeyboardInterrupt:
                logger.info("策略停止")
                break
            except Exception as e:
                logger.error(f"主循环异常: {e}")
                time.sleep(60)


def main():
    """主入口"""
    try:
        with open('config.v93innovation.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
    except FileNotFoundError:
        logger.error("配置文件不存在: config.v93innovation.json")
        return
    except json.JSONDecodeError:
        logger.error("配置文件格式错误")
        return

    config['standalone'] = True  # 强制开启独立模式组件
    if 'port' not in config:
        config['port'] = 5090

    strategy = V93Strategy(config)
    strategy.run()


if __name__ == "__main__":
    main()