"""
实盘引擎 (ETH Swap 适配版)
"""
import time
import threading
import json
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional, Callable, Any

from core import (
    MarketData, Signal, Order, FillEvent, Position,
    StrategyContext, PortfolioSnapshot, OrderStatus
)
from strategies import BaseStrategy
from executors import BaseExecutor
from datafeeds import BaseDataFeed
from services.history import HistoryService
from services.status import StatusService
from core.event_bus import bus
from core.dto import CandleEvent, ResetEvent, FillEventPayload

class LiveEngine:
    def __init__(self,
                 strategy: BaseStrategy,
                 executor: BaseExecutor,
                 data_feed: BaseDataFeed,
                 warmup_bars: int = 100,
                 data_suffix: str = ""):
        self.strategy = strategy
        self.executor = executor
        self.data_feed = data_feed
        self.warmup_bars = warmup_bars
        
        self.is_running = False
        self._is_warmed = False
        self._current_time: Optional[datetime] = None
        self._current_prices: Dict[str, float] = {}
        self.initial_total_value: float = 0.0
        self._history_candles: List[Dict] = []
        self._history_rsi: List[float] = []
        self._history_equity: List[Dict] = []
        
        # 引入服务化组件
        self.history = HistoryService(symbol=data_feed.symbol, data_suffix=data_suffix)
        self.status_svc = StatusService(executor=executor, strategy=strategy)
        
        # 初始资金加载逻辑移至下方 reconstruct_state 之后
        
        self._status_callbacks: List[Callable[[Dict], None]] = []
        self._should_restart = False 
        self._pending_intents: Dict[str, Dict] = {} 
        self.state_snapshot: Dict[str, Any] = {}
        
        self.last_ui_minute = -1 
        
        # 2026-03-28 修复：对于 Paper 模式，加载交易记录后立即重建执行器状态
        if getattr(self.executor, 'uid', '') == 'PaperAccount':
            if hasattr(self.executor, 'reconstruct_state'):
                # 2026-04-03 优先全量重建成交记录，这是最准确的
                self.executor.reconstruct_state(self.history._trades)
            
            # 随后加载快照仅用于恢复策略内部状态（网格等），而不应覆盖现金
            state_snapshot = getattr(self, 'state_snapshot', None) or self._load_paper_state_snapshot()
            if hasattr(self.executor, 'apply_account_snapshot'):
                # 注意：apply_account_snapshot 内部现在应该更小心，不要覆盖已重建的 cash
                # 或者在此处直接调用策略的恢复逻辑
                self.strategy.restore_snapshot(state_snapshot)
                print(f"[引擎] Paper 账户已依交易记录重建，策略状态已从快照恢复")
            
        # 2026-04-01 修复增强版：建立初始资金基准线
        if self.history.initial_total_value and self.history.initial_total_value > 0:
            self.initial_total_value = self.history.initial_total_value
            print(f"[引擎] 已从历史记录加载初始计资金: {self.initial_total_value:.2f} USDT")
        else:
            # 首次启动，以当前账户总权益作为基准 (通常为 10000.0)
            self.initial_total_value = self.executor.get_total_value()
            if self.initial_total_value > 0:
                self.history.save_initial_balance(self.initial_total_value)
                print(f"[引擎] 首次启动，已建立并固化初始资金基准线: {self.initial_total_value:.2f} USDT")
            
        self._sync_exchange_orders()   # 同步委托单历史
        
        self.executor.register_fill_callback(self._on_fill)
        self.data_feed.register_data_callback(self._on_data)
    
    def _load_paper_state_snapshot(self) -> Dict[str, Any]:
        state_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'v95_state.json')
        if not os.path.exists(state_file):
            return {}
        try:
            with open(state_file, 'r', encoding='utf-8') as f:
                return json.load(f) or {}
        except Exception as e:
            print(f"[??] ?? paper ??????: {e}")
            return {}

    def _sync_exchange_orders(self):
        """同步委托单级别的历史记录 (避免扫单导致记录过多)"""
        try:
            if getattr(self.executor, 'uid', '') == 'PaperAccount': return

            print(f"[引擎] 正在从 OKX 同步 {self.data_feed.symbol} 的委托单记录...")
            raw_orders = self.executor.get_order_history(self.data_feed.symbol, limit=50)
            if not raw_orders: return

            new_count = 0
            has_changes = False
            existing_ids = {t.get('meta', {}).get('ord_id') for t in self.history._trades if t.get('meta', {}).get('ord_id')}
            
            for o in raw_orders:
                ord_id = o.get('ordId')
                if ord_id in existing_ids: continue
                fill_sz = float(o.get('fillSz', 0))
                if fill_sz <= 0: continue

                ts_ms = int(o.get('uTime') or o.get('cTime') or (time.time() * 1000))
                dt = datetime.fromtimestamp(ts_ms/1000, tz=timezone.utc)
                side = o.get('side', '').upper()
                pos_side = o.get('posSide', '').lower()
                avg_px = float(o.get('avgPx', 0))
                pnl = float(o.get('pnl', 0)) if o.get('pnl') else None
                
                action = ("开多" if side == 'BUY' else "平多") if pos_side == 'long' else (("开空" if side == 'SELL' else "平空") if pos_side == 'short' else ("买入" if side == 'BUY' else "卖出"))

                cl_ord_id = o.get('clOrdId')
                intent = self._pending_intents.get(cl_ord_id, {}) if cl_ord_id else {}
                grid_level = intent.get('level', '-')
                
                detail = f"[同步] {action} | 层级:{grid_level} | 数量={fill_sz:.4f} 均价={avg_px:.2f}"
                if intent.get('reason'): detail += f" | 理由: {intent['reason']}"
                
                eth_sz = fill_sz * self.executor.ct_val
                existing_trade = next((t for t in self.history._trades if t['meta'].get('ord_id') == ord_id), None)
                
                if existing_trade:
                    existing_trade.update({'price': avg_px, 'size': eth_sz, 'quote_amount': avg_px * eth_sz, 'pnl': pnl, 't': ts_ms, 'time': dt.isoformat(), 'detail': detail})
                    has_changes = True
                    continue
                
                self.history.add_trade({
                    'type': side, 'action': action, 'symbol': self.data_feed.symbol,
                    'price': avg_px, 'size': eth_sz, 'quote_amount': avg_px * eth_sz, 
                    'pnl': pnl, 'time': dt.isoformat(), 't': ts_ms, 'detail': detail,
                    'reason': intent.get('reason') or "交易所同步",
                    'meta': {'ord_id': ord_id, 'cl_ord_id': cl_ord_id, 'source': 'exchange', 'strategy_meta': intent.get('meta', {})}
                })
                new_count += 1
                has_changes = True
                if cl_ord_id in self._pending_intents: del self._pending_intents[cl_ord_id]
            
            if has_changes:
                self.history._trades.sort(key=lambda x: x['t'])
                self.history.save_trades()
                if new_count > 0: print(f"[引擎] 已同步加载 {new_count} 条新委托单记录")
        except Exception as e:
            print(f"[引擎] 同步委托单历史失败: {e}")

    def _sync_equity_history(self):
        """通过账单还原与 K 线对齐的资产历史 (阶梯形态)"""
        try:
            if not self.history._history_candles: return

            # 纸笔交易模式下，通过本地初始资产填充曲线即可
            if getattr(self.executor, 'uid', '') == 'PaperAccount':
                account_val = self.executor.get_total_value()
                self.history.sync_equity_history_from_bills([], account_val, self.history.last_reset_t)
                return

            print(f"[引擎] 正在从 OKX 账单中还原资产历史...")
            bills = self.executor.get_recent_bills(limit=200)
            account_val = self.executor.get_total_value()
            self.history.sync_equity_history_from_bills(bills, account_val, self.history.last_reset_t)
        except Exception as e:
            print(f"[引擎] 还原资产历史失败: {e}")
    
    def register_status_callback(self, callback: Callable[[Dict], None]):
        self._status_callbacks.append(callback)
    
    def _get_context(self) -> StrategyContext:
        self.status_svc.update_cache()
        positions = {f"{p.symbol}_{'long' if p.size > 0 else 'short'}": p for p in self.status_svc._cached_positions}
        meta = {'total_equity': self.status_svc._cached_total_value}
        if hasattr(self.executor, 'get_account_snapshot'):
            try:
                meta.update(self.executor.get_account_snapshot())
            except Exception:
                pass
        return StrategyContext(
            timestamp=self._current_time,
            cash=self.status_svc._cached_cash,
            positions=positions,
            current_prices=self._current_prices.copy(),
            meta=meta
        )
    
    def _on_fill(self, fill: FillEvent):
        """处理成交事件：发布事件由订阅者处理"""
        self.status_svc.update_cache(force=True)
        self.strategy.on_fill(fill, self._get_context())
        
        # 发布成交事件给 HistoryService 和 StatusService (以及 UI 等)
        bus.publish("fill_event", FillEventPayload(fill=fill))

        cl_ord_id = fill.meta.get('clOrdId') or fill.order_id
        if cl_ord_id in self._pending_intents: del self._pending_intents[cl_ord_id]
        
        # 实时同步交易所记录
        threading.Timer(0.5, self._sync_exchange_orders).start()
    
    def _on_data(self, data: MarketData):
        """处理实时数据"""
        self._current_time = data.timestamp
        self._current_prices[data.symbol] = data.close
        self.executor.update_market_data(data.timestamp, data.close)
        
        # 发布行情更新事件
        # 这会自动触发 HistoryService 的 K线/资产 同步，以及 StatusService 的缓存刷新
        bus.publish("candle_update", CandleEvent(
            symbol=data.symbol,
            data=data,
            equity=self.status_svc._cached_total_value,
            rsi=getattr(self.strategy, 'last_rsi', 50)
        ))
        
        # 执行策略逻辑
        context = self._get_context()
        signals = self.strategy.on_data(data, context)
        if signals: self._execute_signals(signals)
            
        if hasattr(context, 'meta') and 'requested_leverage' in context.meta:
            lev = context.meta['requested_leverage']
            if lev > 0: self.executor.set_leverage(lev)
        
        # 每 30 次轮询同步一次交易所订单
        if not hasattr(self, '_data_count'): self._data_count = 0
        self._data_count += 1
        if self._data_count % 30 == 0: self._sync_exchange_orders()
            
        status = self.status_svc.build_status(
            data=data,
            initial_balance=self.initial_total_value,
            history_equity=self._history_equity,
            current_prices=self._current_prices
        )
        # 2026-04-01 修复：合并历史载荷 (K线、资产曲线、成交记录等)
        # get_history_payload 会根据内部标志位决定是否包含全量或增量数据
        payload = self.history.get_history_payload(max_points=500)
        status.update(payload)
        
        self._notify_status(status)
        
        current_min = datetime.now().minute
        if current_min != self.last_ui_minute:
            self.last_ui_minute = current_min
    
    def _execute_signals(self, signals: List[Signal]):
        for signal in signals:
            order = Order(
                order_id="",
                symbol=signal.symbol,
                side=signal.side,
                size=signal.size,
                order_type=signal.order_type,
                price=signal.price,
                timestamp=signal.timestamp,
                meta=signal.meta
            )
            # 记录意图，以便稍后同步时回填理由
            cl_ord_id = order.meta.get('clOrdId')
            if not cl_ord_id:
                cl_ord_id = f"v95x{int(time.time()*1000)}"
                order.meta['clOrdId'] = cl_ord_id
                
            self._pending_intents[cl_ord_id] = {
                'reason': signal.reason,
                'level': signal.meta.get('level'),
                'meta': signal.meta
            }
            
            order_id = self.executor.submit_order(order)
            if not order_id or order.status == OrderStatus.REJECTED:
                # 如果下单失败，移除意图缓存
                if cl_ord_id in self._pending_intents:
                    del self._pending_intents[cl_ord_id]
                reason = order.meta.get('reject_reason', 'submit_failed_or_rejected')
                print(f"[执行拒单] {order.symbol} {order.side.value} sz={order.size} reason={reason}")
    
    def _notify_status(self, data: Dict):
        for callback in self._status_callbacks:
            try:
                callback(data)
            except Exception as e:
                print(f"状态回调错误: {e}")
    def run(self):
        """启动引擎并进入监控循环"""
        self.is_running = True
        print(f"\n[引擎] >>> {self.strategy.name} 运行中 <<<")
        print(f"[引擎] 交易对: {self.strategy.symbol}")
        print(f"[引擎] 执行器: {type(self.executor).__name__}")
        
        # 1. 执行历史数据预热
        self.warmup()
        
        # 2. 启动实时数据流轮询 (阻塞模式)
        try:
            for _ in self.data_feed.stream():
                if not self.is_running:
                    break
        except KeyboardInterrupt:
            print("\n[引擎] 接收到停止指令")
        except Exception as e:
            print(f"\n[引擎] 运行异常: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self.stop()

    def warmup(self):
        """通用预热逻辑"""
        self.strategy.initialize()
        print(f"正在预热策略 ({self.warmup_bars} bars)...")
        
        try:
            if hasattr(self.data_feed, 'api'):
                df = self.data_feed.api.get_candles(
                    self.data_feed._inst_id, 
                    self.data_feed._bar_map.get(self.data_feed.timeframe, '1m'), 
                    limit=self.warmup_bars
                )
                if df is not None and len(df) > 0:
                    print(f"  成功获取 {len(df)} 条历史数据")
                    # 预热阶段的资产点：
                    # 如果已知初始资金 (4999.x)，我们应该用它作为这段“远古”历史的起点
                    # 只有在确实没有初始记录时才用当前值兜底。这消除了从 5000 到当前值的平线跳变。
                    historical_equity = self.initial_total_value if (self.initial_total_value and self.initial_total_value > 0) else self.executor.get_total_value()
                        
                    # 2026-03-28 修复：预热阶段使用空持仓，避免策略在回放历史K线时
                    # 基于真实持仓产出虚假的开仓/平仓/拦截信号日志，误导用户判断。
                    # 2026-04-03 修复：同时显式标记 warmup 模式，避免策略把空持仓当成真实状态，
                    # 反向清空从 state 快照恢复出的 executor/strategy 持仓。
                    warmup_positions = {}  # 空持仓，防止预热期产出虚假信号
                        
                    for timestamp, row in df.iterrows():
                        data = MarketData(
                            timestamp=timestamp, symbol=self.data_feed.symbol,
                            open=float(row['open']), high=float(row['high']),
                            low=float(row['low']), close=float(row['close']),
                            volume=float(row['vol'])
                        )
                        # 重要：预热阶段调用 on_data 以初始化 RSI 和波段点等内部状态
                        # 使用预热专用的 Mock Context (空持仓)
                        context = StrategyContext(
                            timestamp=timestamp,
                            cash=historical_equity, # 预热期暂用初始值
                            positions=warmup_positions,
                            current_prices={data.symbol: data.close}, # 补全缺失参数，防止 TypeError
                            meta={'warmup': True, 'total_equity': historical_equity}
                        )
                        self.strategy.on_data(data, context)
                        
                        # 同步到内部历史记录（用于预热摘要展示）
                        candle_dict = {
                            't': int(data.timestamp.timestamp() * 1000),
                            'o': data.open, 'h': data.high, 'l': data.low, 'c': data.close, 'v': data.volume
                        }
                        self._history_candles.append(candle_dict)
                        # 保留最后一根 K 线用于后续状态组装
                        last_data = data
                        
                        # 发布行情更新事件（预热期）
                        bus.publish("candle_update", CandleEvent(
                            symbol=data.symbol,
                            data=data,
                            equity=historical_equity,
                            rsi=getattr(self.strategy, 'last_rsi', 50)
                        ))
                    
                    # 预热结束，给 Dashboard 发送一次全量同步，确保 K 线完整
                    if 'last_data' in locals():
                        payload = self.history.get_history_payload(max_points=500)
                        status = self.status_svc.build_status(
                            data=last_data,
                            initial_balance=self.initial_total_value,
                            history_equity=[], # build_status 内部会自行合并，这里传空即可
                            current_prices=self._current_prices
                        )
                        # 核心：将 HistoryService 中的全量历史覆盖进 status
                        status.update(payload)
                        self._notify_status(status)
                        print(f"  [同步] 已向 Dashboard 推送 {len(payload.get('history_candles', []))} 根预热 K 线")

                    # 预热结束，输出摘要
                    if len(self._history_candles) > 0:
                        first = self._history_candles[0]
                        last = self._history_candles[-1]
                        f_t = datetime.fromtimestamp(first['t']/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
                        l_t = datetime.fromtimestamp(last['t']/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
                        print(f"  [摘要] 预热起点: {f_t} | 价格: {first['c']:.2f}")
                        print(f"  [摘要] 预热终点: {l_t} | 价格: {last['c']:.2f}")
                else:
                    print(f"  警告: 未能获取历史数据 (可能网络超时或 API 错误)，尝试继续运行...")
        except Exception as e:
            print(f"  预热过程中发生异常: {e}")
            import traceback; traceback.print_exc()
        
        self._is_warmed = True # 标记预热流程已执行完成 (无论成功与否，以免阻塞主循环)
    
    def _save_initial_balance(self):
        """将初始余额持久化"""
        if self.initial_total_value:
            self.history.save_initial_balance(self.initial_total_value)
            
        if not self._is_warmed:
            self.warmup()
            # 预热完成后，且已载入 K 线骨架，执行资产历史对齐还原
        self._sync_equity_history()
        
        self.is_running = True
        self.strategy.on_start()
        
        print("  开始正式运行...")
        print(f"\n{'='*60}\n实盘引擎启动 | 策略: {self.strategy.name}\n{'='*60}\n")
        
        try:
            data_count = 0
            for data in self.data_feed.stream():
                if not self.is_running: break
                
                data_count += 1
                # 使用统一的 _on_data 处理每根 K 线
                self._on_data(data)
                
                # 移除冗余的每10条数据处理打印
                # if data_count % 10 == 0:
                #     print(f"[引擎] 已处理 {data_count} 条数据 | 价格: {data.close:.2f}")
                
        except KeyboardInterrupt:
            print("\n收到停止信号...")
        except Exception as e:
            print(f"引擎错误: {e}")
        finally:
            self.stop()

    def reset(self):
        """重置引擎状态 (V93 改进版)"""
        print("\n" + "!"*40 + "\n[引擎] 正在执行重置程序 (同步资产模式)...\n" + "!"*40)
        
        # 1. 获取当前最新权益并设为重置本金，并重置执行器本身
        try:
            # 2026-03-28 修复：重置时必须同时重置执行器的持仓和现金
            if hasattr(self.executor, 'reset'):
                self.executor.reset()
            
            current_equity = self.executor.get_total_value()
            # 发布重置事件，由各服务自行清理内存和持久化文件
            bus.publish("reset_event", ResetEvent(
                t=int(time.time() * 1000),
                time=datetime.now(timezone.utc).isoformat(),
                equity=current_equity,
                reason="manual_reset"
            ))
            
            # 保存初始资金
            self._save_initial_balance()
        except Exception as e:
            print(f"[引擎] 重置过程异常: {e}")

        # 2. 停止引擎
        self.stop()
        
        # 3. 清理内存状态
        self._should_restart = True # 标记需要重启
        
        # 4. 重置策略内部状态
        if hasattr(self.strategy, 'initialize'):
            self.strategy.initialize()
            
        print(f"[引擎] 重置完成。初始本金已设为: {self.initial_total_value:.2f} USDT")
        print("[引擎] 准备重新启动以对齐历史显示...")
        
        # 注意：这里并不自动 run()，而是让外部逻辑决定
        # 但在 run_eth_swap.py 中，由于 run() 在主线程，重置建议通过设置标志位或拋出异常实现
        # 简化版：这里只清理数据，由 LiveEngine.run() 中的循环检查 stop 状态

    def stop(self):
        self.is_running = False
        self.strategy.on_stop()
        self.data_feed.stop()
        print("\n引擎已停止")
