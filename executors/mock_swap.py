"""
模拟合约执行器 (用于 Paper Test) - 增强版
职责：在本地模拟下单、双向持仓计算、盈亏统计。
"""
import time
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from core import Order, FillEvent, Position, OrderStatus, Side, OrderType
from .base import BaseExecutor

class MockSwapExecutor(BaseExecutor):
    def __init__(self, initial_cash: float = 10000.0, ct_val: float = 0.1):
        super().__init__()
        self.cash = initial_cash
        self.initial_balance = initial_cash
        self.ct_val = ct_val
        self.uid = "PaperAccount"
        
        # 双向持仓记录 { (symbol, posSide): Position }
        self.positions: Dict[tuple, Position] = {}
        self.current_prices: Dict[str, float] = {}
        self._order_map: Dict[str, Order] = {}
        self.leverage = 3.0 # 默认 3x

    def _eth_to_sz(self, eth_amount: float) -> int:
        return int(round(eth_amount / self.ct_val))

    def submit_order(self, order: Order) -> str:
        inst_id = order.symbol
        side = order.side.value # 'buy' or 'sell'
        pos_side = order.meta.get('posSide', 'long') # 'long' or 'short'
        sz_contracts = self._eth_to_sz(order.size)
        real_sz_eth = sz_contracts * self.ct_val
        
        # 获取当前价格进行模拟成交
        price = self.current_prices.get(inst_id, order.price or 0.0)
        if price <= 0:
            print(f"[Paper][警告] 价格无效 ({price})，无法执行订单")
            return ""

        # 模拟订单 ID
        ord_id = f"mock_{int(time.time()*1000)}"
        order.order_id = ord_id
        
        # 计算手续费 (模拟 OKX 0.05%)
        fee_rate = 0.0005
        fee = price * real_sz_eth * fee_rate
        
        # 判断是开仓还是平仓 (对盈亏计算很重要)
        # 买入开多 (Buy Long), 卖出平多 (Sell Long)
        # 卖出开空 (Sell Short), 买入平空 (Buy Short)
        is_open = (side == 'buy' and pos_side == 'long') or (side == 'sell' and pos_side == 'short')
        
        # 盈亏计算
        pnl = 0.0
        if not is_open:
            # 2026-03-28 修复：安全检查，防止在没有持仓时执行“平仓”逻辑导致反向开仓
            pos_key = (inst_id, pos_side)
            existing_pos = self.positions.get(pos_key)
            if not existing_pos or abs(existing_pos.size) < 1e-8:
                print(f"[Paper][拒单] {reason} | 尝试{action}但当前没有相关方向的持仓")
                order.status = OrderStatus.REJECTED
                order.meta['reject_reason'] = f"No {pos_side} position to close"
                return ""

            # 平仓时计算已实现盈亏
            direction = 1 if pos_side == 'long' else -1
            pnl = (price - existing_pos.avg_price) * real_sz_eth * direction
            # 扣减手续费
            pnl -= fee
            print(f"[Paper][平仓] {pos_side} | 成交价:{price:.2f} | 均价:{existing_pos.avg_price:.2f} | 已实现盈亏: {pnl:.2f}")

        # 意图记录
        reason = order.meta.get('reason', 'Paper Test')
        action = ("开" if is_open else "平") + ("多" if pos_side == 'long' else "空")
        print(f"[Paper][下单] {reason} | {action} {inst_id} sz={sz_contracts}张 (${price:.2f})")
        
        # 模拟成交事件
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
        
        # 更新本地持仓和现金
        self._update_local_account(fill, is_open)
        
        # 哪怕是开仓也要扣手续费
        if is_open:
            self.cash -= fee
        else:
            # 平仓时现金增加 (包含已实现盈亏)
            # 实际上在合约中，盈利是增加权益。这里简化为将盈亏直接计入现金
            self.cash += pnl 

        order.status = OrderStatus.FILLED
        self._notify_fill(fill)
        
        return ord_id

    def _update_local_account(self, fill: FillEvent, is_open: bool):
        symbol = fill.symbol
        pos_side = fill.meta.get('posSide', 'long')
        pos_key = (symbol, pos_side)
        
        pos = self.positions.get(pos_key, Position(symbol=symbol, size=0, avg_price=0, entry_time=datetime.now(timezone.utc)))
        
        # 双向持仓逻辑
        if is_open:
            # 开仓/加仓
            new_size_abs = abs(pos.size) + fill.filled_size
            new_avg_px = (abs(pos.size) * pos.avg_price + fill.filled_size * fill.filled_price) / new_size_abs
            
            # size 我们统一用正数表示绝对持仓，方向由 posSide 决定（或者多正空负）
            # 为了兼容 LiveEngine 之前的显示逻辑，我们用：多头为正，空头为负
            final_size = new_size_abs if pos_side == 'long' else -new_size_abs
        else:
            # 平仓/减仓
            new_size_abs = max(0, abs(pos.size) - fill.filled_size)
            if new_size_abs < 1e-8:
                new_avg_px = 0
                final_size = 0
            else:
                new_avg_px = pos.avg_price # 平仓不移动均价
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
        # 优先返回有持仓的那个（双向由于策略目前可能只持有一侧）
        for (sym, side), pos in self.positions.items():
            if sym == symbol and abs(pos.size) > 1e-8:
                return pos
        return None

    def get_all_positions(self) -> List[Position]:
        return [p for p in self.positions.values() if abs(p.size) > 1e-8]

    def reset(self):
        """完全重置执行器状态"""
        self.positions = {}
        self.cash = self.initial_balance
        print(f"[Paper] 执行器已完全重置。本金恢复至: {self.cash:.2f} USDT")

    def get_cash(self) -> float:
        """
        返回‘可用资金’ (Free Margin = Equity - Margin)
        符合交易所习惯，防止仪表盘显示金额不合理。
        """
        equity = self.get_total_value()
        margin = self.get_total_margin()
        return max(0, equity - margin)

    def get_total_margin(self) -> float:
        """计算当前所有持仓占用的总保证金 (按 3x 杠杆)"""
        margin = 0
        for p in self.positions.values():
            if abs(p.size) < 1e-8: continue
            price = self.current_prices.get(p.symbol, p.avg_price)
            margin += (abs(p.size) * price) / self.leverage
        return margin

    def get_total_value(self) -> float:
        """获取账户总价值 (Equity = Cash + Unrealized PnL)"""
        total = self.cash # 此处的 cash 实际上是 Balance (含已实现盈亏)
        for (symbol, pos_side), p in self.positions.items():
            if abs(p.size) < 1e-8: continue
            
            price = self.current_prices.get(symbol, p.avg_price)
            direction = 1 if pos_side == 'long' else -1
            u_pnl = (price - p.avg_price) * abs(p.size) * direction
            total += u_pnl
        return total

    def update_market_data(self, timestamp: datetime, price: float):
        self.current_prices['ETH-USDT-SWAP'] = price
        self.current_prices['ETH/USDT'] = price
        # 同时也通过基类方法分发（如果有订阅者）
        super().update_market_data(timestamp, price)

    def set_leverage(self, leverage: float):
        self.leverage = leverage
        print(f"[Paper] 策略请求杠杆调整为: {leverage}x (本地已记录)")

    def get_recent_fills(self, symbol: str, limit: int = 100) -> List[Dict]:
        return []

    def get_order_history(self, symbol: str, limit: int = 100) -> List[Dict]:
        return []

    def get_recent_bills(self, limit: int = 100) -> List[Dict]:
        return []

    def reconstruct_state(self, trade_records: List[Dict]):
        """从历史交易记录重建执行器状态 (用于重启后的数据对齐)"""
        if not trade_records:
            return
            
        print(f"[Paper] 正在从 {len(trade_records)} 条历史记录重建持仓状态...")
        # 重置当前状态
        self.positions = {}
        self.cash = self.initial_balance
        
        # 按时间排序确保按序执行
        sorted_trades = sorted(trade_records, key=lambda x: x.get('t', 0))
        
        # 2026-03-31 增强：引入 ID 去重机制，防止重复记录导致“幽灵持仓”
        seen_ord_ids = set()
        dedup_count = 0

        for trade in sorted_trades:
            # 获取唯一标识 (优先使用 ord_id，其次使用 trade_id)
            ord_id = trade.get('meta', {}).get('ord_id') or trade.get('meta', {}).get('trade_id')
            if ord_id:
                if ord_id in seen_ord_ids:
                    dedup_count += 1
                    continue
                seen_ord_ids.add(ord_id)

            symbol = trade.get('symbol')
            side = trade.get('type', '').lower() # 'buy' or 'sell'
            action = trade.get('action', '')
            price = float(trade.get('price', 0))
            size = float(trade.get('size', 0))
            pnl = float(trade.get('pnl', 0))
            
            # 判断 posSide
            pos_side = 'long'
            if '短' in action or '空' in action or trade.get('meta', {}).get('posSide') == 'short':
                pos_side = 'short'
            
            # 判断是否为开仓
            is_open = '开' in action or 'entry' in trade.get('reason', '').lower()
            
            # 这里我们手动更新本地账户逻辑，不触发 notify_fill
            pos_key = (symbol, pos_side)
            pos = self.positions.get(pos_key, Position(symbol=symbol, size=0, avg_price=0, entry_time=datetime.now(timezone.utc)))
            
            # 双向持仓逻辑
            if is_open:
                new_size_abs = abs(pos.size) + size
                if new_size_abs > 0:
                    new_avg_px = (abs(pos.size) * pos.avg_price + size * price) / new_size_abs
                else:
                    new_avg_px = 0
                final_size = new_size_abs if pos_side == 'long' else -new_size_abs
                # 扣除手续费 (近似计算)
                self.cash -= price * size * 0.0005
            else:
                new_size_abs = max(0, abs(pos.size) - size)
                if new_size_abs < 1e-8:
                    new_avg_px = 0
                    final_size = 0
                else:
                    new_avg_px = pos.avg_price
                    final_size = new_size_abs if pos_side == 'long' else -new_size_abs
                # 平仓增加现金 (包含已实现盈亏)
                self.cash += pnl
                
            self.positions[pos_key] = Position(
                symbol=symbol,
                size=final_size,
                avg_price=new_avg_px,
                entry_time=datetime.now(timezone.utc)
            )
            
        if dedup_count > 0:
            print(f"[Paper] 已自动剔除 {dedup_count} 条历史重复成交记录")
            
        print(f"[Paper] 重建完成。有效持仓方向: {len(self.get_all_positions())}, 可用资金: {self.get_cash():.2f}")
