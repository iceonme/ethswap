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
        self._equity_curve: List[PortfolioSnapshot] = []
        self._trades: List[Dict] = []
        self._history_candles: List[Dict] = []
        self._status_callbacks: List[Callable[[Dict], None]] = []
        self._history_sent = False # 记录历史数据是否已同步通过
        self._history_equity: List[Dict] = [] # 记录资产历史
        self._history_rsi: List[float] = []   # 记录 RSI 历史 (对齐用)
        self._latest_trade: Optional[Dict] = None # 最近一条记录
        self._should_restart = False # 是否需要重启
        self._pending_intents: Dict[str, Dict] = {} # 意图缓存 clOrdId -> {reason, meta}
        # 资产统计
        self.initial_total_value: Optional[float] = None
        # 交易持久化 (支持后缀隔离和目录规范化)
        suffix_str = f"_{data_suffix}" if data_suffix else ""
        data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
        if not os.path.exists(data_dir): os.makedirs(data_dir)
        
        self.trades_file = os.path.join(data_dir, f"v93_trades{suffix_str}.json")
        self.initial_balance_file = os.path.join(data_dir, f"v93_initial_balance{suffix_str}.json")
        self.reset_history_file = os.path.join(data_dir, f"v93_reset_history{suffix_str}.json")
        print(f"[DEBUG] Engine Balance File Path: {os.path.abspath(self.initial_balance_file)}")
        
        self.last_reset_t = 0
        self.last_ui_minute = -1 # 用于控制 UI 推送频率 (1分钟/次)
        self._load_reset_history()     # 加载重置历史以确定过滤起点
        self._load_trades()
        
        # 2026-03-28 修复：对于 Paper 模式，加载交易记录后立即重建执行器状态，防止重启后持仓丢失
        if getattr(self.executor, 'uid', '') == 'PaperAccount' and hasattr(self.executor, 'reconstruct_state'):
            self.executor.reconstruct_state(self._trades)
            
        self._load_initial_balance()   # 从文件或调试日志恢复初始资金
        self._sync_exchange_orders()   # 同步委托单历史 (替代之前的成交明细同步)
        # 注意：此处不再调用 _sync_equity_history，改为在 warmup 之后调用，以利用已载入的 K 线对齐
        
        self.executor.register_fill_callback(self._on_fill)
        self.data_feed.register_data_callback(self._on_data)
    
    def _load_trades(self):
        """从文件加载交易记录"""
        try:
            if os.path.exists(self.trades_file):
                with open(self.trades_file, 'r', encoding='utf-8') as f:
                    self._trades = json.load(f)
                
                # 2026-03-31 修复：启动时自动清理重复记录
                original_count = len(self._trades)
                seen_ids = set()
                unique_trades = []
                for t in self._trades:
                    oid = t.get('meta', {}).get('ord_id') or t.get('meta', {}).get('trade_id')
                    if oid not in seen_ids:
                        unique_trades.append(t)
                        if oid: seen_ids.add(oid)
                    else:
                        continue
                
                if len(unique_trades) < original_count:
                    self._trades = unique_trades
                    print(f"[引擎][清理] 已从历史记录中清理 {original_count - len(unique_trades)} 条重复交易")
                    self._save_trades()
                
                print(f"[引擎] 已加载 {len(self._trades)} 条历史交易记录")
        except Exception as e:
            print(f"[引擎] 加载交易历史失败: {e}")
            self._trades = []

    def _save_trades(self):
        """保存交易记录到文件"""
        try:
            with open(self.trades_file, 'w', encoding='utf-8') as f:
                json.dump(self._trades, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"[引擎] 保存交易历史失败: {e}")

    def _sync_exchange_orders(self):
        """同步委托单级别的历史记录 (避免扫单导致记录过多)"""
        try:
            # 纸笔交易模式下，不需要从 OKX 同步委托单
            if getattr(self.executor, 'uid', '') == 'PaperAccount':
                return

            print(f"[引擎] 正在从 OKX 同步 {self.data_feed.symbol} 的委托单记录...")
            raw_orders = self.executor.get_order_history(self.data_feed.symbol, limit=50)
            if not raw_orders:
                return

            new_count = 0
            has_changes = False
            # 建立现有 ordId 集合以去重
            existing_ids = set()
            for t in self._trades:
                oid = t.get('meta', {}).get('ord_id')
                if oid: existing_ids.add(oid)
            
            for o in raw_orders:
                ord_id = o.get('ordId')
                if ord_id in existing_ids:
                    continue
                
                # 只同步已成交部分大于0的订单
                fill_sz = float(o.get('fillSz', 0))
                if fill_sz <= 0:
                    continue

                u_time = o.get('uTime')
                c_time = o.get('cTime')
                if u_time:
                    ts_ms = int(u_time)
                elif c_time:
                    ts_ms = int(c_time)
                else:
                    ts_ms = int(time.time() * 1000)
                dt = datetime.fromtimestamp(ts_ms/1000, tz=timezone.utc)
                side = o.get('side', '').upper()
                pos_side = o.get('posSide', '').lower()
                avg_px = float(o.get('avgPx', 0))
                pnl = float(o.get('pnl', 0)) if o.get('pnl') else None
                
                # 翻译 Action
                if pos_side == 'long':
                    action = "开多" if side == 'BUY' else "平多"
                elif pos_side == 'short':
                    action = "开空" if side == 'SELL' else "平空"
                else:
                    action = "买入" if side == 'BUY' else "卖出"

                # 关联本地意图 (Reason, Level 等)
                cl_ord_id = o.get('clOrdId')
                intent = self._pending_intents.get(cl_ord_id, {}) if cl_ord_id else {}
                
                # 记录理由增强
                strategy_reason = intent.get('reason') or "交易所同步"
                grid_level = intent.get('level', '-')
                
                detail = f"[同步] {action} | 层级:{grid_level} | 数量={fill_sz:.4f} 均价={avg_px:.2f}"
                if intent.get('reason'):
                   detail += f" | 理由: {intent['reason']}"
                
                # 将张数转换为 ETH 数量，确保单位统一
                eth_sz = fill_sz * self.executor.ct_val

                # 检查是否已存在记录
                existing_trade = next((t for t in self._trades if t['meta'].get('ord_id') == ord_id), None)
                
                if existing_trade:
                    # 如果已存在（如实时成交生成的瞬态记录），则更新 PnL 和精确数据
                    existing_trade.update({
                        'price': avg_px,
                        'size': eth_sz,
                        'quote_amount': avg_px * eth_sz,
                        'pnl': pnl,
                        'reason': f"{existing_trade.get('reason', '')} [已同步]"
                    })
                    # 确保更新后的记录时间戳也是最新的
                    existing_trade['t'] = ts_ms
                    existing_trade['time'] = dt.isoformat()
                    # 更新 detail 字段
                    existing_trade['detail'] = detail
                    # 更新 meta 中的 strategy_meta
                    existing_trade['meta']['strategy_meta'] = intent.get('meta', {})
                    has_changes = True
                    # 不计入 new_count，因为是更新
                    # print(f"[引擎] 更新了委托单记录 {ord_id}")
                    continue # 继续处理下一个 raw_order
                
                record = {
                    'type': side,
                    'action': action,
                    'symbol': self.data_feed.symbol,
                    'price': avg_px,
                    'size': eth_sz, # 统一使用 ETH 数量
                    'quote_amount': avg_px * eth_sz, 
                    'pnl': pnl,
                    'time': dt.isoformat(),
                    't': ts_ms,
                    'detail': detail,
                    'reason': strategy_reason,
                    'meta': {
                        'ord_id': ord_id, 
                        'cl_ord_id': cl_ord_id,
                        'source': 'exchange',
                        'strategy_meta': intent.get('meta', {})
                    }
                }
                self._trades.append(record)
                new_count += 1
                has_changes = True
                
                # 同步成功后，如果缓存还在，可以移除 (可选，也可保留一段时间)
                if cl_ord_id in self._pending_intents:
                    del self._pending_intents[cl_ord_id]
            
            if has_changes:
                self._trades.sort(key=lambda x: x['t'])
                self._save_trades()
                self._history_sent = False # 关键修复：允许下一次轮询推送全量历史给 Dashboard
                if new_count > 0:
                    print(f"[引擎] 已同步加载 {new_count} 条新委托单记录")
                else:
                    print(f"[引擎] 已更新现有委托单状态 (PnL/Price)")
        except Exception as e:
            print(f"[引擎] 同步委托单历史失败: {e}")

    def _load_initial_balance(self):
        """从文件恢复初始资金"""
        try:
            # 1. 尝试从正式持久化文件读取
            if os.path.exists(self.initial_balance_file):
                with open(self.initial_balance_file, 'r') as f:
                    data = json.load(f)
                val = data.get('initial_balance')
                # 2025-03-23 优化：放宽本金恢复范围检查 (> 0 且不等于已知的错误大数值)
                if val and val > 0 and val != 84000:
                    self.initial_total_value = val
                    print(f"[引擎] 已从持久化文件恢复初本金: {self.initial_total_value:.2f}")
                    return
                else:
                    print(f"[引擎] 持久化文件中的数值异常 ({val})，将尝试从备份/调试文件恢复")

            # 2. 尝试从 tmp/debug_equity.json 读取 (用于找回用户提到的 4999.x)
            # 路径修正：从 ethswap/engines/live.py 回溯三层到根目录，再进入 tmp
            root_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            debug_file = os.path.join(root_dir, "tmp", "debug_equity.json")
            if os.path.exists(debug_file):
                with open(debug_file, 'r') as f:
                    data = json.load(f)
                if isinstance(data, list) and len(data) > 0:
                    val = data[0].get('v')
                    if val and val > 0:
                        self.initial_total_value = val
                        print(f"[引擎] 已从调试文件还原初本金: {self.initial_total_value:.2f}")
                        self._save_initial_balance() # 顺便存入正式文件
                        return
                        
        except Exception as e:
            print(f"[引擎] 加载初始资金失败: {e}")

    def _save_initial_balance(self):
        """持久化初始资金"""
        if self.initial_total_value is None or self.initial_total_value <= 0:
            return
        try:
            with open(self.initial_balance_file, 'w') as f:
                json.dump({'initial_balance': self.initial_total_value}, f)
            print(f"[引擎] 初始本金已持久化: {self.initial_total_value:.2f}")
        except Exception as e:
            print(f"[引擎] 保存初始资金失败: {e}")

    def _load_reset_history(self):
        """记录重置历史事件，并获取最后一次重置时间"""
        try:
            if os.path.exists(self.reset_history_file):
                with open(self.reset_history_file, 'r', encoding='utf-8') as f:
                    history = json.load(f)
                if history and isinstance(history, list):
                    last_event = history[-1]
                    self.last_reset_t = last_event.get('t', 0)
                    print(f"[引擎] 已加载重置历史，最近重置点: {datetime.fromtimestamp(self.last_reset_t/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")
        except Exception as e:
            print(f"[引擎] 加载重置历史失败: {e}")

    def _save_reset_event(self, equity: float, reason: str = "manual_reset"):
        """保存一次新的重置事件"""
        try:
            history = []
            if os.path.exists(self.reset_history_file):
                with open(self.reset_history_file, 'r', encoding='utf-8') as f:
                    history = json.load(f)
            
            ts_ms = int(time.time() * 1000)
            self.last_reset_t = ts_ms
            
            history.append({
                't': ts_ms,
                'time': datetime.now(timezone.utc).isoformat(),
                'equity': equity,
                'reason': reason
            })
            
            with open(self.reset_history_file, 'w', encoding='utf-8') as f:
                json.dump(history, f, indent=4, ensure_ascii=False)
            print(f"[引擎] 已记录重置事件: {equity:.2f} USDT at {ts_ms}")
        except Exception as e:
            print(f"[引擎] 保存重置事件失败: {e}")

    def _sync_equity_history(self):
        """通过账单还原与 K 线对齐的资产历史 (阶梯形态)"""
        try:
            if not self._history_candles:
                return

            # 纸笔交易模式下，通过本地初始资产填充曲线即可，无需请求 OKX 账单
            if getattr(self.executor, 'uid', '') == 'PaperAccount':
                base_val = self.initial_total_value if (self.initial_total_value and self.initial_total_value > 0) else self.executor.get_total_value()
                equity_history = []
                for candle in self._history_candles:
                    equity_history.append({'t': candle['t'], 'v': base_val})
                self._history_equity = equity_history
                # print(f"[引擎] Paper模式：已采用基准资产填充历史曲线: {base_val:.2f}")
                return

            print(f"[引擎] 正在从 OKX 账单中还原资产历史...")
            bills = self.executor.get_recent_bills(limit=200)
            
            equity_history = []
            if not bills:
                # 如果没有账单，全量使用初始资产或当前资产填充以对齐 K 线
                base_val = self.initial_total_value if (self.initial_total_value and self.initial_total_value > 0) else self.executor.get_total_value()
                for candle in self._history_candles:
                    equity_history.append({'t': candle['t'], 'v': base_val})
                self._history_equity = equity_history
                print(f"[引擎] 未找到账单，已采用基准资产填充曲线: {base_val:.2f}")
                return

            # 按时间排序 bills (从旧到新)
            sorted_bills = sorted(bills, key=lambda x: int(x['ts']))
            
            bill_idx = 0
            # 2025-03-25 改进：初始锚点必须止步于最近一次重置。
            # 如果 K 线在重置时间之前，则统一采用重置点作为资产基准 (避免出现阶梯跳变)
            current_bal = self.initial_total_value if (self.initial_total_value and self.initial_total_value > 0) else float(sorted_bills[0].get('bal', 0))
            
            for candle in self._history_candles:
                candle_ts = candle['t']
                
                # 如果当前 K 线在重置前，强制使用 initial_total_value (即平线)
                if candle_ts < self.last_reset_t:
                    target_bal = self.initial_total_value
                else:
                    # 获取该 K 线时间或之前的最新一笔账单
                    while bill_idx < len(sorted_bills) and int(sorted_bills[bill_idx]['ts']) <= candle_ts:
                        # 仅处理重置点后的有效账单
                        if int(sorted_bills[bill_idx]['ts']) >= self.last_reset_t:
                            current_bal = float(sorted_bills[bill_idx].get('bal', 0))
                        bill_idx += 1
                    target_bal = current_bal
                
                equity_history.append({
                    't': candle_ts,
                    'v': target_bal
                })
            
            if equity_history:
                self._history_equity = equity_history
                # 如果依然没有初始资产定义，则将其锁定为曲线第一个点
                if self.initial_total_value is None or self.initial_total_value <= 0:
                    self.initial_total_value = equity_history[0]['v']
                    self._save_initial_balance()
                print(f"[引擎] 已还原 {len(equity_history)} 个对齐的资产快照点，初始资产: {self.initial_total_value:.2f}")
        except Exception as e:
            print(f"[引擎] 还原资产历史失败: {e}")
            import traceback; traceback.print_exc()
    
    def register_status_callback(self, callback: Callable[[Dict], None]):
        self._status_callbacks.append(callback)
    
    def _get_context(self) -> StrategyContext:
        positions = {}
        for pos in self.executor.get_all_positions():
            # 修复：使用 (symbol, pos_side) 唯一键，防止多空持仓在 dict 中互相覆盖导致逻辑失效
            key = f"{pos.symbol}_{'long' if pos.size > 0 else 'short'}"
            positions[key] = pos
            
        # 2026-03-25 改进：提供真正的账户总权益给策略 (用于动态分仓计算)
        account_equity = getattr(self, '_cached_total_value', self.executor.get_total_value())

        return StrategyContext(
            timestamp=self._current_time,
            cash=self.executor.get_cash(),
            positions=positions,
            current_prices=self._current_prices.copy(),
            meta={'total_equity': account_equity}
        )
    
    def _on_fill(self, fill: FillEvent):
        self.strategy.on_fill(fill)
        side = fill.side.value.upper()
        symbol = fill.symbol
        price = fill.filled_price
        size = fill.filled_size
        quote_amount = fill.quote_amount
        
        # 合约语义适配：size 可能为负（对应 Position），但在交易记录中我们显示绝对值
        abs_size = abs(size)
        
        # 合约特有：Action 翻译 (开多/平多/开空/平空)
        pos_side = fill.meta.get('posSide', '').lower()
        if pos_side == 'long':
            action = "开多" if side == 'BUY' else "平多"
        elif pos_side == 'short':
            action = "开空" if side == 'SELL' else "平空"
        else:
            # 兼容逻辑
            action = "买入" if side == 'BUY' else "卖出"

        detail = f"{action} 数量={abs_size:.4f} 价格={price:.2f}"
        
        # 2025-03-25 修复：增加平仓单盈亏计算逻辑，用于展示胜率
        pnl = fill.pnl
        if pnl is None and ("平" in action or "卖出" in action):
            # 尝试从当前持仓缓存中获取均价进行计算
            # 优先使用实时同步的 executor 仓位数据
            current_pos = None
            if "多" in action:
                current_pos = next((p for p in self.executor.get_all_positions() if p.size > 0), None)
            elif "空" in action:
                current_pos = next((p for p in self.executor.get_all_positions() if p.size < 0), None)
            
            if current_pos and current_pos.avg_price > 0:
                # PNL = (平仓价 - 开仓价) * 数量 * 方向
                direction = 1 if "多" in action else -1
                pnl = (price - current_pos.avg_price) * abs_size * direction
                print(f"[盈亏计算] {action} 均价: {current_pos.avg_price:.2f} 当前: {price:.2f} PNL: {pnl:.2f}")

        # 计算保证金占用 (用于前端显示 "交易金额" 时符合用户感知的 300 多 U)
        margin = (abs_size * price) / getattr(self.strategy, 'current_leverage', 3.0)
        
        trade_record = {
            'type': side,
            'action': action,
            'symbol': symbol,
            'price': price,
            'size': abs_size,
            'quote_amount': quote_amount, # 名义价值
            'margin': margin,             # 实际占用保证金
            'pnl': pnl,
            'time': fill.timestamp.isoformat(),
            't': int(fill.timestamp.timestamp() * 1000), 
            'detail': detail,
            'reason': fill.meta.get('reason', '实时交易'),
            'meta': {
                'trade_id': fill.meta.get('trade_id') or str(int(time.time()*1000)), 
                'ord_id': fill.order_id, 
                'source': 'live'
            }
        }
        # 2026-03-31 修复：对于本地/模拟成交，直接将记录存入本地 trades 列表并持久化
        # 因为在 Paper 模式下，_sync_exchange_orders 不会从交易所拉取到任何数据
        if fill.meta.get('source') != 'exchange':
            # 2026-03-31 修复：去重检查，防止同一笔订单被记录多次
            ord_id = trade_record['meta'].get('ord_id')
            if ord_id and any(t.get('meta', {}).get('ord_id') == ord_id for t in self._trades):
                print(f"[引擎][去重] 订单 {ord_id} 已在历史记录中，跳过重复写入")
            else:
                self._trades.append(trade_record)
                self._save_trades()
                # 2026-03-28 修复：成交后立即重置同步计时器，确保 Dashboard 立即看到最新持仓和资金
                self._last_account_sync = 0 
                self._history_sent = False # 触发 Dashboard 增量同步
                print(f"[实盘记录] 已将本地成交存入历史: {detail}")

        # 实时触发订单同步 (针对实盘运行)，确保服务器端的 PnL 等数据最终对齐
        # 同时清理意图缓存，防止内存泄漏
        cl_ord_id = fill.meta.get('clOrdId') or fill.order_id
        if cl_ord_id in self._pending_intents:
            del self._pending_intents[cl_ord_id]

        if not isinstance(self.executor, BaseExecutor): # Placeholder for checking if real
             threading.Timer(1.0, self._sync_exchange_orders).start()
        else:
             # 如果是模拟执行器，也触发一次同步以清理意图缓存 (虽然在 submit_order 已经清理了部分)
             threading.Timer(0.5, self._sync_exchange_orders).start()
    
    def _on_data(self, data: MarketData):
        """处理实时数据 (主要由 run 循环调用)"""
        # 数据量计数
        if not hasattr(self, '_data_count'): self._data_count = 0
        self._data_count += 1
        
        self._current_time = data.timestamp
        self._current_prices[data.symbol] = data.close
        
        # 将最新价格同步给执行器 (MockExecutor 需要此数据来模拟成交)
        self.executor.update_market_data(data.timestamp, data.close)
        
        # 同步资产和 RSI 历史 (传入最新的从交易所获取的实时资产，而非静态缓存)
        # 注意：此处使用经过 10s 刷新的 _cached_total_value 以平滑曲线
        latest_equity = getattr(self, '_cached_total_value', self.initial_total_value)
        self._sync_history_candles(data, current_equity=latest_equity)
        
        # 执行策略逻辑
        context = self._get_context()
        signals = self.strategy.on_data(data, context)
        
        if signals:
            self._execute_signals(signals)
            
        # 杠杆动态调整逻辑 (V93 专用)
        if hasattr(context, 'meta') and isinstance(context.meta, dict) and 'requested_leverage' in context.meta:
            lev = context.meta['requested_leverage']
            if lev > 0:
                self.executor.set_leverage(lev)
                print(f"[引擎] 策略请求调整杠杆为: {lev}x")
        
        # 每 30 次数据包(约30-40秒)自动触发一次交易所订单同步
        if self._data_count % 30 == 0:
            self._sync_exchange_orders()
            
        # 2025-03-25 优化：移除 1 分钟推送限制，改为实时推送 (约 2s 一次)
        # 满足用户“每秒跳一次价”的需求，同时也让 K 线看起来更连贯
        status = self._build_status(data)
        self._notify_status(status)
        
        # 仅保留日志频率限制，避免刷屏
        current_min = datetime.now().minute
        if current_min != getattr(self, 'last_ui_minute', -1):
            self.last_ui_minute = current_min
            # 移除冗余的UI同步打印
            # print(f"[UI] 已完成常规分钟级同步点 (Minute: {current_min})")
    
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
                cl_ord_id = f"v93x{int(time.time()*1000)}"
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
                    # 预热的目的仅仅是初始化 RSI、网格等内部状态，不应触发任何交易逻辑。
                    warmup_positions = {}  # 空持仓，防止预热期产出虚假信号
                        
                    for timestamp, row in df.iterrows():
                        data = MarketData(
                            timestamp=timestamp, symbol=self.data_feed.symbol,
                            open=float(row['open']), high=float(row['high']),
                            low=float(row['low']), close=float(row['close']),
                            volume=float(row['volume'])
                        )
                        # 重要：预热阶段调用 on_data 以初始化 RSI 和波段点等内部状态
                        # 使用预热专用的 Mock Context (空持仓)
                        context = StrategyContext(
                            timestamp=timestamp,
                            cash=historical_equity, # 预热期暂用初始值
                            positions=warmup_positions,
                            current_prices={data.symbol: data.close} # 补全缺失参数，防止 TypeError
                        )
                        self.strategy.on_data(data, context)
                        
                        self._current_prices[data.symbol] = data.close
                        # 传递 historical_equity 填充数据骨架
                        self._sync_history_candles(data, current_equity=historical_equity)
                    
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
    
    def run(self):
        # 记录初始本金 (如果尚未通过持久化恢复)
        if self.initial_total_value is None:
            self.initial_total_value = self.executor.get_total_value()
            self._save_initial_balance()
            print(f"[引擎] 记录初始本金: {self.initial_total_value:.2f} USDT")
            
        if not self._is_warmed:
            self.warmup()
            # 预热完成后，且已载入 K 线骨架，执行资产历史对齐还原
            self._sync_equity_history()
        
        self.is_running = True
        self.strategy.on_start()
        
        print("  开始正式运行...")
        
        # 预热后立即推送一次当前状态给 Dashboard，以显示历史数据
        if self._history_candles:
            # 使用最后一根预热 K 线作为基础
            last_candle = self._history_candles[-1]
            # 构造一个临时 MarketData 用于 status 构造
            last_data = MarketData(
                timestamp=datetime.fromtimestamp(last_candle['t']/1000, tz=timezone.utc),
                symbol=self.data_feed.symbol,
                open=last_candle['o'], high=last_candle['h'],
                low=last_candle['l'], close=last_candle['c'], volume=0
            )
            status = self._build_status(last_data)
            self._notify_status(status)
            print("  已同步历史数据到监控面板")

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
            import traceback; traceback.print_exc()
        finally:
            self.stop()
    
    def _build_status(self, data: MarketData) -> Dict:
        # 2025-03-23 优化：缓存账户资产数据，避免 2s 一次的频繁 API 请求导致刷新卡顿
        now = time.time()
        # 2026-03-28 优化：从 10s 降低到 3s，提高持仓变动灵敏度
        if not hasattr(self, '_last_account_sync') or (now - self._last_account_sync > 3):
            try:
                self._cached_positions = self.executor.get_all_positions()
                self._cached_cash = self.executor.get_cash()
                self._cached_total_value = self.executor.get_total_value()
                self._last_account_sync = now
                # print(f"[状态] 已更新账户资产和持仓数据 (下一步每 2s 仅更新价格)")
            except Exception as e:
                print(f"更新账户状态失败(使用缓存): {e}")
                if not hasattr(self, '_cached_positions'):
                    self._cached_positions = []
                    self._cached_cash = 0
                    self._cached_total_value = 0
        
        positions = self._cached_positions
        account_cash = self._cached_cash
        account_total_value = self._cached_total_value

        # 主卡片收益率口径：只按策略专属 USDT 基准计算，不用账户级 USDT 覆盖
        if self._history_equity:
            strategy_total_value = float(self._history_equity[-1]['v'])
        else:
            strategy_total_value = self.initial_total_value if self.initial_total_value is not None else account_total_value

        total_value = account_total_value
        cash = account_cash
        
        # 计算 PNL（策略专属 USDT 口径）
        pnl_pct = 0.0
        if self.initial_total_value and self.initial_total_value > 0:
            pnl_pct = (account_total_value - self.initial_total_value) / self.initial_total_value * 100
        
        strategy_status = {}
        if hasattr(self.strategy, 'get_status'):
            strategy_status = self.strategy.get_status()
        
        # 2026-03-28 修复：支持双向持仓在 Dashboard 上的正确显示 (改为数组)
        pos_map = {}
        for p in positions:
            if p.symbol not in pos_map:
                pos_map[p.symbol] = []
            pos_map[p.symbol].append({
                'size': p.size,
                'avg_price': p.avg_price,
                'value': p.size * data.close if p.size != 0 else 0
            })
            
        status = {
            'timestamp': data.timestamp.isoformat(),
            'symbol': data.symbol,
            'price': data.close,
            'candle': {
                't': int(data.timestamp.timestamp() * 1000),
                'o': data.open, 'h': data.high, 'l': data.low, 'c': data.close
            },
            'prices': self._current_prices.copy(),
            'cash': cash,           # 使用上文已定义的 cash (即 Available)
            'total_value': total_value, # 使用上文已定义的 total_value (即 Equity)
            'initial_balance': self.initial_total_value,
            'pnl_pct': pnl_pct,
            'rsi': strategy_status.get('rsi', 50),
            'strategy': strategy_status,
            'uid': getattr(self.executor, 'uid', 'Unknown'),
            'positions': pos_map,
            # 2026-03-28 新增：显式汇总指标，作为前端显示的唯一事实来源
            'total_margin': float(getattr(self.executor, 'get_total_margin', lambda: 0)()),
            'account_cash': float(account_cash)
        }
        
        # 增量发送最新交易
        if self._latest_trade:
            status['trade'] = self._latest_trade
            self._latest_trade = None # 发送后清空
            
        # 仅在第一次同步时同步庞大的历史数据 (统一取最后 500 点以对齐索引)
        max_hist = 500
        if not self._history_sent:
            if self._history_candles:
                # K线历史可以保留较长，方便查看大趋势
                status['history_candles'] = self._history_candles[-max_hist:]
            if self._history_equity:
                # 2025-03-25 改进：不再对视觉图表进行过滤，确保刷新后能看到预热历史
                status['history_equity'] = self._history_equity[-max_hist:]
            if self._history_rsi:
                status['history_rsi'] = self._history_rsi[-max_hist:]
            if self._trades:
                # 2025-03-25 改进：根据最后一次重置时间过滤交易记录显示，并保留历史数据
                filtered_trades = [t for t in self._trades if t.get('t', 0) >= self.last_reset_t]
                status['trade_history'] = filtered_trades
                
            self._history_sent = True
            print(f"[引擎] 已完成初始全量数据同步 (对齐点数: {len(status.get('history_candles', []))}, 过滤后交易数: {len(status.get('trade_history', []))})")
            
        return status
    
    def _sync_history_candles(self, data: MarketData, current_equity: float = None):
        """同步 K 线、资产及 RSI 历史，确保索引严格对齐。
        注意：history_equity 必须始终使用策略专属 USDT 口径，不得回退到账户级 total_value。
        """
        candle_ms = int(data.timestamp.timestamp() * 1000)
        candle = {
            't': candle_ms,
            'o': data.open, 'h': data.high, 'l': data.low, 'c': data.close
        }

        # 获取当前权益和 RSI
        if current_equity is not None:
            strategy_equity = current_equity
        elif self._history_equity:
            strategy_equity = float(self._history_equity[-1]['v'])
        elif self.initial_total_value is not None:
            strategy_equity = self.initial_total_value
        else:
            strategy_equity = 0

        rsi = getattr(self.strategy, 'last_rsi', 50.0)

        if self._history_candles and self._history_candles[-1]['t'] == candle_ms:
            # 更新当前 K 线 (OHLC 逻辑)
            prev = self._history_candles[-1]
            prev['h'] = max(prev['h'], data.high)
            prev['l'] = min(prev['l'], data.low)
            prev['c'] = data.close
            # open 保持不变 (如果是同一分钟)
            self._history_candles[-1] = prev
            if self._history_equity:
                self._history_equity[-1] = {'t': candle_ms, 'v': strategy_equity}
            if self._history_rsi:
                self._history_rsi[-1] = float(rsi)
        else:
            # 新增 K 线及其对齐点
            self._history_candles.append(candle)
            self._history_equity.append({'t': candle_ms, 'v': strategy_equity})
            self._history_rsi.append(float(rsi))

        if len(self._history_equity) > 500:
            self._history_equity = self._history_equity[-500:]
        if len(self._history_rsi) > 500:
            self._history_rsi = self._history_rsi[-500:]
        if len(self._history_candles) > 2000:
            self._history_candles = self._history_candles[-2000:]

    def save_trades(self, filepath: str):
        import json
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(self._trades, f, indent=4)
        except Exception as e:
            print(f"[引擎] 保存交易记录失败: {e}")

    def load_trades(self, filepath: str):
        import json, os
        if not os.path.exists(filepath): return
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                self._trades = json.load(f)
        except Exception as e:
            print(f"[引擎] 加载交易记录失败: {e}")

    def reset(self):
        """重置引擎状态 (V93 改进版)"""
        print("\n" + "!"*40 + "\n[引擎] 正在执行重置程序 (同步资产模式)...\n" + "!"*40)
        
        # 1. 获取当前最新权益并设为重置本金，并重置执行器本身
        try:
            # 2026-03-28 修复：重置时必须同时重置执行器的持仓和现金
            if hasattr(self.executor, 'reset'):
                self.executor.reset()
            
            current_equity = self.executor.get_total_value()
            self.initial_total_value = current_equity
            self._save_initial_balance()
            self._save_reset_event(current_equity, reason="manual_reset")
            
            # 同时物理删除交易记录文件，确保真正的“从零开始”
            if os.path.exists(self.trades_file):
                try:
                    os.remove(self.trades_file)
                    self._trades = []
                    print(f"[引擎] 已物理删除交易历史文件: {self.trades_file}")
                except Exception as ex:
                    print(f"[引擎] 删除历史文件失败: {ex}")
        except Exception as e:
            print(f"[引擎] 重置过程异常: {e}")

        # 2. 停止引擎
        self.stop()
        
        # 3. 清理内存状态 (不需要清空 self._trades，因为我们要保留历史并通过 last_reset_t 过滤)
        self._history_candles = []
        self._history_equity = []
        self._history_rsi = []
        self._history_sent = False
        
        self._should_restart = True # 标记需要重启
        
        # 4. 重置策略内部状态
        if hasattr(self.strategy, 'initialize'):
            self.strategy.initialize()
            
        print(f"[引擎] 重置完成。初始本金已设为: {self.initial_total_value:.2f} USDT")
        print("[引擎] 准备重新启动以对齐历史显示...")
        
        # 注意：这里并不自动 run()，而是让外部逻辑决定
        # 但在 run_eth_swap_v93.py 中，由于 run() 在主线程，重置建议通过设置标志位或拋出异常实现
        # 简化版：这里只清理数据，由 LiveEngine.run() 中的循环检查 stop 状态

    def stop(self):
        self.is_running = False
        self.strategy.on_stop()
        self.data_feed.stop()
        print("\n引擎已停止")
