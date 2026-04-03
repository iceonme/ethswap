import time
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional
from core import StrategyContext, PortfolioSnapshot, MarketData, Position
from core.event_bus import bus
from core.dto import CandleEvent, FillEventPayload

class StatusService:
    def __init__(self, executor: Any, strategy: Any):
        self.executor = executor
        self.strategy = strategy
        
        self._cached_positions: List[Any] = []
        self._cached_cash: float = 0.0
        self._cached_total_value: float = 0.0
        self._last_account_sync: float = 0
        
        self._setup_subscriptions()

    def _setup_subscriptions(self):
        bus.subscribe("candle_update", self._on_candle_update)
        bus.subscribe("fill_event", self._on_fill_event)

    def _on_candle_update(self, event: CandleEvent):
        # 实时数据驱动下，如果没到定时刷新点，可以选择静默，或者强制刷新
        self.update_cache()

    def _on_fill_event(self, payload: FillEventPayload):
        # 成交后强制刷新缓存
        self.update_cache(force=True)
        
    def update_cache(self, force: bool = False):
        now = time.time()
        if force or (now - self._last_account_sync > 3):
            try:
                self._cached_positions = self.executor.get_all_positions()
                self._cached_cash = self.executor.get_cash()
                self._cached_total_value = self.executor.get_total_value()
                self._last_account_sync = now
            except Exception as e:
                print(f"[StatusService] 更新账户缓存失败: {e}")

    def build_status(self, data: MarketData, initial_balance: Optional[float], 
                     history_equity: List[Dict], current_prices: Dict[str, float]) -> Dict:
        try:
            self.update_cache()
            
            # 收益率计算
            current_equity = self._cached_total_value
            pnl_pct = 0.0
            if initial_balance and initial_balance > 0:
                pnl_pct = (current_equity - initial_balance) / initial_balance * 100
            unrealized_pnl = float(getattr(self.executor, 'get_unrealized_pnl', lambda: 0.0)() or 0.0)
            position_market_value = float(getattr(self.executor, 'get_position_market_value', lambda: 0.0)() or 0.0)
            realized_pnl = float(getattr(self.executor, 'get_realized_pnl', lambda: current_equity - float(initial_balance or 0.0) - unrealized_pnl)() or 0.0)
                
            strategy_status = {}
            if hasattr(self.strategy, 'get_status'):
                try:
                    strategy_status = self.strategy.get_status()
                except Exception as e:
                    print(f"[StatusService] 获取策略状态失败: {e}")
                
            # 持仓按 Symbol 映射
            pos_map = {}
            for p in self._cached_positions:
                if p.symbol not in pos_map:
                    pos_map[p.symbol] = []
                # 保护 p.size, p.avg_price, data.close 为 None 的情况
                size = float(p.size or 0)
                avg_px = float(p.avg_price or 0)
                cur_px = float(data.close or 0)
                pos_map[p.symbol].append({
                    'size': size,
                    'avg_price': avg_px,
                    'value': size * cur_px if size != 0 else 0
                })
                
            status = {
                'time': datetime.now().strftime('%H:%M:%S'),
                'timestamp': data.timestamp.astimezone(timezone.utc).isoformat() if data.timestamp and data.timestamp.tzinfo else (data.timestamp.isoformat() if data.timestamp else datetime.now(timezone.utc).isoformat()),
                'symbol': data.symbol,
                'price': float(data.close or 0),
                'candle': {
                    't': int(data.timestamp.astimezone(timezone.utc).timestamp() * 1000) if data.timestamp and data.timestamp.tzinfo else (int(data.timestamp.timestamp()*1000) if data.timestamp else int(time.time()*1000)),
                    'o': float(data.open or 0), 'h': float(data.high or 0), 
                    'l': float(data.low or 0), 'c': float(data.close or 0)
                },
                'prices': current_prices,
                'cash': float(self._cached_cash or 0),
                'total_value': float(current_equity or 0),
                'initial_balance': initial_balance,
                'pnl_pct': float(pnl_pct or 0),
                'realized_pnl': float(realized_pnl or 0),
                'unrealized_pnl': float(unrealized_pnl or 0),
                'total_fees': float(getattr(self.executor, 'get_total_fees', lambda: 0)()),
                'position_market_value': float(position_market_value or 0),
                'rsi': float(strategy_status.get('rsi', 50) if strategy_status else 50),
                'strategy': strategy_status,
                'uid': str(getattr(self.executor, 'uid', 'Unknown')),
                'positions': pos_map,
                'total_margin': float(getattr(self.executor, 'get_total_margin', lambda: 0)()),
                'account_cash': float(self._cached_cash or 0),
                'available_cash': float(getattr(self.executor, 'get_available_cash', lambda: self._cached_cash or 0)())
            }
            return status
        except Exception as e:
            print(f"[StatusService] build_status 全局失败: {e}")
            import traceback
            traceback.print_exc()
            return {'error': str(e)}
