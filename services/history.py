import os
from datetime import datetime, timezone
from typing import List, Dict, Optional, Any
from core.dto import TradeRecord, ResetEvent, CandleEvent, FillEventPayload
from core.event_bus import bus
from repositories.trade_repo import JSONTradeRepository
from repositories.account_repo import JSONAccountRepository

class HistoryService:
    def __init__(self, symbol: str, data_suffix: str = ""):
        self.symbol = symbol
        self._history_candles: List[Dict] = []
        self._history_equity: List[Dict] = []
        self._history_rsi: List[float] = []
        self._history_sent = False
        
        # 初始化仓库
        suffix_str = f"_{data_suffix}" if data_suffix else ""
        data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
        self.trade_repo = JSONTradeRepository(os.path.join(data_dir, f"v95_trades{suffix_str}.json"))
        self.account_repo = JSONAccountRepository(
            balance_file=os.path.join(data_dir, f"v95_initial_balance{suffix_str}.json"),
            reset_file=os.path.join(data_dir, f"v95_reset_history{suffix_str}.json")
        )
        
        self.last_reset_t = 0
        self.initial_total_value: Optional[float] = None
        self._trades: List[Dict] = []
        
        self.load_all()
        self._setup_subscriptions()

    def _setup_subscriptions(self):
        bus.subscribe("candle_update", self._on_candle_update)
        bus.subscribe("fill_event", self._on_fill_event)
        bus.subscribe("reset_event", self._on_reset_event)

    def _on_candle_update(self, event: CandleEvent):
        self.sync_history_candles(event.data, event.equity, event.rsi)

    def _on_fill_event(self, payload: FillEventPayload):
        fill = payload.fill
        abs_size = abs(fill.filled_size)
        pos_side = fill.meta.get('posSide', '').lower()
        if pos_side == 'long':
            action = "开多" if fill.side.value.upper() == 'BUY' else "平多"
        elif pos_side == 'short':
            action = "开空" if fill.side.value.upper() == 'SELL' else "平空"
        else:
            action = "买入" if fill.side.value.upper() == 'BUY' else "卖出"

        detail = f"{action} 数量={abs_size:.4f} 均价={fill.filled_price:.2f}"
        pnl = fill.pnl # 简化处理由引擎或执行器计算
        
        self.add_trade({
            'type': fill.side.value.upper(), 'action': action, 'symbol': fill.symbol,
            'price': fill.filled_price, 'size': abs_size, 'quote_amount': fill.quote_amount,
            'pnl': pnl, 'time': fill.timestamp.isoformat(),
            't': int(fill.timestamp.timestamp() * 1000), 'detail': detail,
            'reason': fill.meta.get('reason', '实时交易'),
            'meta': {'trade_id': fill.meta.get('trade_id'), 'ord_id': fill.order_id, 'source': 'live'}
        })

    def _on_reset_event(self, event: ResetEvent):
        self.last_reset_t = event.t
        self._history_candles = []
        self._history_equity = []
        self._history_rsi = []
        self._history_sent = False

    def load_all(self):
        # 加载重置历史
        resets = self.account_repo.load_reset_history()
        if resets:
            self.last_reset_t = resets[-1].t
            
        # 加载初始资金
        self.initial_total_value = self.account_repo.load_initial_balance()
        
        # 加载交易记录 (转换为字典以保持与现有 Dashboard 兼容)
        trade_objs = self.trade_repo.load_all()
        self._trades = [t.to_dict() for t in trade_objs]

    def save_trades(self):
        trade_objs = [TradeRecord(**t) for t in self._trades]
        self.trade_repo.save_all(trade_objs)

    def save_initial_balance(self, value: float):
        self.initial_total_value = value
        self.account_repo.save_initial_balance(value)

    def save_reset_event(self, equity: float, reason: str = "manual_reset"):
        ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        self.last_reset_t = ts_ms
        event = ResetEvent(
            t=ts_ms,
            time=datetime.now(timezone.utc).isoformat(),
            equity=equity,
            reason=reason
        )
        self.account_repo.save_reset_event(event)

    def sync_history_candles(self, data: Any, current_equity: float, strategy_rsi: float = 50):
        ts_ms = int(data.timestamp.astimezone(timezone.utc).timestamp() * 1000) if data.timestamp.tzinfo else int(data.timestamp.timestamp() * 1000)
        if not self._history_candles or self._history_candles[-1]['t'] != ts_ms:
            self._history_candles.append({
                't': ts_ms, 'o': data.open, 'h': data.high, 'l': data.low, 'c': data.close
            })
            self._history_equity.append({'t': ts_ms, 'v': current_equity})
            self._history_rsi.append(strategy_rsi)
        else:
            self._history_candles[-1].update({'h': data.high, 'l': data.low, 'c': data.close})
            self._history_equity[-1]['v'] = current_equity
            self._history_rsi[-1] = strategy_rsi

        if len(self._history_candles) > 1000:
            self._history_candles = self._history_candles[-1000:]
            self._history_equity = self._history_equity[-1000:]
            self._history_rsi = self._history_rsi[-1000:]

    def sync_equity_history_from_bills(self, bills: List[Dict], current_account_value: float, last_reset_t: int):
        if not self._history_candles: return
        
        history_equity = []
        if not bills:
            base_val = self.initial_total_value or current_account_value
            for candle in self._history_candles:
                history_equity.append({'t': candle['t'], 'v': base_val})
            self._history_equity = history_equity
            return

        sorted_bills = sorted(bills, key=lambda x: int(x['ts']))
        bill_idx = 0
        current_bal = self.initial_total_value or float(sorted_bills[0].get('bal', 0))
        
        for candle in self._history_candles:
            candle_ts = candle['t']
            if candle_ts < last_reset_t:
                target_bal = self.initial_total_value
            else:
                while bill_idx < len(sorted_bills) and int(sorted_bills[bill_idx]['ts']) <= candle_ts:
                    if int(sorted_bills[bill_idx]['ts']) >= last_reset_t:
                        current_bal = float(sorted_bills[bill_idx].get('bal', 0))
                    bill_idx += 1
                target_bal = current_bal
            
            history_equity.append({'t': candle_ts, 'v': target_bal})
        
        if history_equity:
            self._history_equity = history_equity
            if not self.initial_total_value:
                self.save_initial_balance(history_equity[0]['v'])

    def add_trade(self, trade_record: Dict):
        self._trades.append(trade_record)
        self.save_trades()
        self._history_sent = False

    def get_history_payload(self, max_points: int = 500) -> Dict:
        res = {}
        # 1. K 线历史 (始终返回，确保 Dashboard 刷新或重连后能立即补全图表)
        res['history_candles'] = self._history_candles[-max_points:]
        res['history_equity'] = self._history_equity[-max_points:]
        res['history_rsi'] = self._history_rsi[-max_points:]
        
        # 2. 交易记录 & 初始资金 (数据量小，且对 UI 关键，每次 payload 请求都返回)
        # 这样即使 K 线已发，重新连接或刷新时的首包也能拿到成交历史
        res['trade_history'] = [t for t in self._trades if t.get('t', 0) >= self.last_reset_t]
        res['initial_balance'] = self.initial_total_value
        
        return res
