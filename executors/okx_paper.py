"""
模拟执行器 (用于 Paper Test) - 增强版
处理现金、持仓、盈亏计算及状态重建
"""
import time
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from core import Order, FillEvent, Position, OrderStatus
from .base import BaseExecutor


class OKXPaperExecutor(BaseExecutor):
    def __init__(self, initial_cash: float = 10000.0, ct_val: float = 0.1):
        super().__init__()
        self.all_time_initial_balance = 10000.0  # 全局初始本金 (固定 10000)
        self.cash = float(initial_cash)  # 当前 USDT 现金 (会随交易扣费/增益而变化)
        self.initial_balance = float(initial_cash) # 当前会话初始现金
        self.ct_val = ct_val
        self.uid = "PaperAccount"

        self.positions: Dict[tuple, Position] = {}
        self.current_prices: Dict[str, float] = {}
        self._order_map: Dict[str, Order] = {}
        self.leverage = 3.0
        self.total_fees = 0.0

    def _eth_to_sz(self, eth_amount: float) -> int:
        return int(round(eth_amount / self.ct_val))

    def submit_order(self, order: Order) -> str:
        inst_id = order.symbol
        side = order.side.value
        pos_side = order.meta.get('posSide', 'long')
        sz_contracts = self._eth_to_sz(order.size)
        real_sz_eth = sz_contracts * self.ct_val

        price = self.current_prices.get(inst_id, order.price or 0.0)
        if price <= 0 or real_sz_eth <= 0:
            print(f"[Paper][报错] 价格或数量无效 | price={price} size={real_sz_eth}")
            return ""

        ord_id = f"mock_{int(time.time() * 1000)}"
        order.order_id = ord_id

        fee_rate = 0.0005
        fee = price * real_sz_eth * fee_rate
        is_open = (side == 'buy' and pos_side == 'long') or (side == 'sell' and pos_side == 'short')
        reason = order.meta.get('reason', 'Paper Test')
        action = ("开" if is_open else "平") + ("多" if pos_side == 'long' else "空")

        pnl = 0.0
        if not is_open:
            pos_key = (inst_id, pos_side)
            existing_pos = self.positions.get(pos_key)
            if not existing_pos or abs(existing_pos.size) < 1e-8:
                print(f"[Paper][拒绝] {reason} | 执行{action}时未找到有效持仓")
                order.status = OrderStatus.REJECTED
                order.meta['reject_reason'] = f"No {pos_side} position to close"
                return ""

            direction = 1 if pos_side == 'long' else -1
            pnl = (price - existing_pos.avg_price) * real_sz_eth * direction - fee
            print(f"[Paper][成交] {pos_side} | 成交价:{price:.2f} | 均价:{existing_pos.avg_price:.2f} | 实现盈亏: {pnl:.2f}")

        print(f"[Paper][下单] {reason} | {action} {inst_id} sz={sz_contracts}张 (${price:.2f})")

        fill = FillEvent(
            order_id=ord_id,
            symbol=inst_id,
            side=order.side,
            filled_size=real_sz_eth,
            filled_price=price,
            timestamp=datetime.now(timezone.utc),
            quote_amount=real_sz_eth * price,
            pnl=pnl,
            meta={'posSide': pos_side, 'source': 'paper', 'reason': reason}
        )

        self._update_local_account(fill, is_open)
        if is_open:
            self.cash -= fee
            self.total_fees += fee
        else:
            self.cash += pnl
            self.total_fees += fee

        order.status = OrderStatus.FILLED
        self._notify_fill(fill)
        return ord_id

    def _update_local_account(self, fill: FillEvent, is_open: bool):
        symbol = fill.symbol
        pos_side = fill.meta.get('posSide', 'long')
        pos_key = (symbol, pos_side)
        pos = self.positions.get(pos_key, Position(symbol=symbol, size=0, avg_price=0, entry_time=datetime.now(timezone.utc)))

        if is_open:
            new_size_abs = abs(pos.size) + fill.filled_size
            new_avg_px = (abs(pos.size) * pos.avg_price + fill.filled_size * fill.filled_price) / new_size_abs
            final_size = new_size_abs if pos_side == 'long' else -new_size_abs
        else:
            new_size_abs = max(0, abs(pos.size) - fill.filled_size)
            if new_size_abs < 1e-8:
                new_avg_px = 0
                final_size = 0
            else:
                new_avg_px = pos.avg_price
                final_size = new_size_abs if pos_side == 'long' else -new_size_abs

        self.positions[pos_key] = Position(
            symbol=symbol,
            size=final_size,
            avg_price=new_avg_px,
            entry_time=datetime.now(timezone.utc),
            unrealized_pnl=0
        )

    def cancel_order(self, order_id: str) -> bool:
        return True

    def get_position(self, symbol: str) -> Optional[Position]:
        for (sym, _), pos in self.positions.items():
            if sym == symbol and abs(pos.size) > 1e-8:
                return pos
        return None

    def get_all_positions(self) -> List[Position]:
        return [p for p in self.positions.values() if abs(p.size) > 1e-8]

    def reset(self):
        self.positions = {}
        self.cash = self.initial_balance
        print(f"[Paper] 执行器已重置为初始状态: {self.cash:.2f} USDT")

    def get_cash(self) -> float:
        return float(self.cash)

    def get_available_cash(self) -> float:
        return max(0.0, self.get_total_value() - self.get_total_margin())

    def get_total_margin(self) -> float:
        margin = 0.0
        for p in self.positions.values():
            if abs(p.size) < 1e-8:
                continue
            price = self.current_prices.get(p.symbol, p.avg_price)
            margin += (abs(p.size) * price) / self.leverage
        return margin

    def get_unrealized_pnl(self) -> float:
        total = 0.0
        for (symbol, pos_side), p in self.positions.items():
            if abs(p.size) < 1e-8:
                continue
            price = self.current_prices.get(symbol, p.avg_price)
            direction = 1 if pos_side == 'long' else -1
            total += (price - p.avg_price) * abs(p.size) * direction
        return total

    def get_realized_pnl(self) -> float:
        # 修改：返回相对于全局初始本金的盈亏
        return float(self.cash - self.all_time_initial_balance)

    def get_total_fees(self) -> float:
        return float(self.total_fees)

    def get_position_market_value(self) -> float:
        total = 0.0
        for (symbol, _), pos in self.positions.items():
            if abs(pos.size) < 1e-8:
                continue
            price = self.current_prices.get(symbol, pos.avg_price)
            total += abs(pos.size) * price
        return float(total)

    def get_account_snapshot(self) -> Dict[str, float]:
        return {
            'cash': float(self.cash),
            'equity': float(self.get_total_value()),
            'unrealized_pnl': float(self.get_unrealized_pnl()),
            'realized_pnl': float(self.get_realized_pnl()),
            'total_fees': float(self.get_total_fees()),
            'position_market_value': float(self.get_position_market_value()),
        }

    def get_total_value(self) -> float:
        return float(self.cash) + float(self.get_unrealized_pnl())

    def update_market_data(self, timestamp: datetime, price: float):
        self.current_prices['ETH-USDT-SWAP'] = price
        self.current_prices['ETH/USDT'] = price
        super().update_market_data(timestamp, price)

    def set_leverage(self, leverage: float):
        self.leverage = leverage
        print(f"[Paper] 杠杆已手动设置为: {leverage}x (仅模拟)")

    def get_recent_fills(self, symbol: str, limit: int = 100) -> List[Dict]:
        return []

    def get_order_history(self, symbol: str, limit: int = 100) -> List[Dict]:
        return []

    def get_recent_bills(self, limit: int = 100) -> List[Dict]:
        return []

    def apply_account_snapshot(self, snapshot: Optional[Dict[str, Any]], latest_price: Optional[float] = None) -> bool:
        if not snapshot:
            return False

        long_position_eth = float(snapshot.get('long_position_eth') or 0.0)
        short_position_eth = float(snapshot.get('short_position_eth') or 0.0)
        total_position_eth = float(snapshot.get('total_position_eth') or long_position_eth or short_position_eth or 0.0)
        long_avg_price = float(snapshot.get('long_avg_price') or 0.0)
        short_avg_price = float(snapshot.get('short_avg_price') or 0.0)
        snapshot_equity = float(snapshot.get('equity') or 0.0)
        snapshot_cash = snapshot.get('cash')
        mark_price = float(snapshot.get('mark_price') or latest_price or 0.0)

        if snapshot_equity <= 0 and snapshot_cash is None and total_position_eth <= 0:
            return False

        self.positions = {}
        if long_position_eth > 1e-8 and long_avg_price > 0:
            self.positions[("ETH-USDT-SWAP", "long")] = Position(symbol="ETH-USDT-SWAP", size=long_position_eth, avg_price=long_avg_price, entry_time=datetime.now(timezone.utc), unrealized_pnl=0)
        if short_position_eth > 1e-8 and short_avg_price > 0:
            self.positions[("ETH-USDT-SWAP", "short")] = Position(symbol="ETH-USDT-SWAP", size=-short_position_eth, avg_price=short_avg_price, entry_time=datetime.now(timezone.utc), unrealized_pnl=0)
        if not self.positions and total_position_eth > 1e-8:
            if long_avg_price > 0:
                self.positions[("ETH-USDT-SWAP", "long")] = Position(symbol="ETH-USDT-SWAP", size=total_position_eth, avg_price=long_avg_price, entry_time=datetime.now(timezone.utc), unrealized_pnl=0)
            elif short_avg_price > 0:
                self.positions[("ETH-USDT-SWAP", "short")] = Position(symbol="ETH-USDT-SWAP", size=-total_position_eth, avg_price=short_avg_price, entry_time=datetime.now(timezone.utc), unrealized_pnl=0)

        if mark_price > 0:
            self.current_prices['ETH-USDT-SWAP'] = mark_price
            self.current_prices['ETH/USDT'] = mark_price

        unrealized = self.get_unrealized_pnl()
        if snapshot_cash is not None:
            self.cash = float(snapshot_cash)
        elif snapshot_equity > 0:
            self.cash = float(snapshot_equity - unrealized)

        if self.cash < 0:
            self.cash = 0.0

        print(f"[Paper] 快照恢复完成 | cash={self.cash:.2f} | equity={self.get_total_value():.2f} | pos={total_position_eth:.4f}")
        return True

    def restore_snapshot(self, snapshot: Optional[Dict[str, Any]], latest_price: Optional[float] = None) -> bool:
        return self.apply_account_snapshot(snapshot=snapshot, latest_price=latest_price)

    def reconstruct_state(self, trade_records: List[Dict]):
        if not trade_records:
            return

        print(f"[Paper] 正在从 {len(trade_records)} 条成交记录中重建账户历史...")
        self.positions = {}
        # 核心修复：重建时应以全局初始本金为起点
        self.cash = self.all_time_initial_balance
        self.total_fees = 0.0

        sorted_trades = sorted(trade_records, key=lambda x: x.get('t', 0))
        seen_ord_ids = set()
        dedup_count = 0

        for trade in sorted_trades:
            ord_id = trade.get('meta', {}).get('ord_id') or trade.get('meta', {}).get('trade_id')
            if ord_id:
                if ord_id in seen_ord_ids:
                    dedup_count += 1
                    continue
                seen_ord_ids.add(ord_id)

            symbol = trade.get('symbol')
            action = trade.get('action', '')
            price = float(trade.get('price') or 0.0)
            size = float(trade.get('size') or 0.0)
            pnl = float(trade.get('pnl') or 0.0)

            pos_side = 'short' if ('空' in action or trade.get('meta', {}).get('posSide') == 'short') else 'long'
            is_open = '开' in action or 'entry' in str(trade.get('reason', '')).lower()

            pos_key = (symbol, pos_side)
            pos = self.positions.get(pos_key, Position(symbol=symbol, size=0, avg_price=0, entry_time=datetime.now(timezone.utc)))

            if is_open or ('买入' in action and pos_side == 'long') or ('卖出' in action and pos_side == 'short'):
                new_size_abs = abs(pos.size) + size
                new_avg_px = ((abs(pos.size) * pos.avg_price + size * price) / new_size_abs) if new_size_abs > 0 else 0.0
                final_size = new_size_abs if pos_side == 'long' else -new_size_abs
                # 扣除开仓手续费
                fee = price * size * 0.0005
                self.cash -= fee
                self.total_fees += fee
            else:
                new_size_abs = max(0.0, abs(pos.size) - size)
                if new_size_abs < 1e-8:
                    new_avg_px = 0.0
                    final_size = 0.0
                else:
                    new_avg_px = pos.avg_price
                    final_size = new_size_abs if pos_side == 'long' else -new_size_abs
                # 加上平仓盈亏 (已在 executor 录入时扣过平仓手续费)
                # 由于平仓 PNL 记录的是净值（已扣除 0.05% 平仓费），
                # 为了统计完整手续费，我们需要在 reconstruct 时还原这一笔。
                exit_fee = price * size * 0.0005
                self.cash += pnl
                self.total_fees += exit_fee

            self.positions[pos_key] = Position(symbol=symbol, size=final_size, avg_price=new_avg_px, entry_time=datetime.now(timezone.utc), unrealized_pnl=0)

        if dedup_count > 0:
            print(f"[Paper] 过滤了 {dedup_count} 条重复成交记录")

        print(f"[Paper] 全量重建完成: cash={self.get_cash():.2f}, equity={self.get_total_value():.2f}")
