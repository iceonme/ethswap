"""
OKX 合约执行器 (ETH Swap 专用)
职责：处理永续合约下单、持仓同步、杠杆设置
"""

import time
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from core import Order, FillEvent, Position, OrderStatus, Side, OrderType
from .base import BaseExecutor
from config.okx_config import OKXAPI


class OKXSwapExecutor(BaseExecutor):
    """
    OKX 合约执行器
    支持全仓模式、双向持仓
    """

    def __init__(self,
                 api_key: str,
                 api_secret: str,
                 passphrase: str,
                 is_demo: bool = True,
                 leverage: int = 1):
        super().__init__()
        self.api = OKXAPI(
            api_key=api_key,
            api_secret=api_secret,
            passphrase=passphrase,
            is_demo=is_demo,
            simulate_slippage=False # 强制发送给 OKX，不使用本地模拟
        )
        self.is_demo = is_demo
        self.leverage = leverage
        self.uid = "Unknown"
        self._order_map: Dict[str, Order] = {}
        
        # 默认合约面值 (ETH-USDT-SWAP 通常是 0.1 ETH/张)
        # 实际运行中会通过 API 获取
        self.ct_val = 0.1 
        
        self._initialize_account()

    def _initialize_account(self):
        """初始化账户设置：设置持仓模式和杠杆"""
        print(f"[执行器] 正在初始化账户设置 (杠杆={self.leverage}x)...")
        try:
            # 0. 获取 UID
            config = self.api.get_account_config()
            if config:
                self.uid = config.get('uid', 'Unknown')
                print(f"[执行器] 识别到账户 UID: {self.uid}")

            # 1. 设置持仓模式为双向 (long_short_mode)
            # V93 逻辑需要明确的 posSide
            res_mode = self.api.set_position_mode('long_short_mode')
            if res_mode and res_mode.get('code') != '0':
                print(f"  注意: 设置持仓模式返回 {res_mode.get('msg')} (可能已设置)")

            # 2. 设置杠杆 (ETH-USDT-SWAP)
            res_lev = self.api.set_leverage('ETH-USDT-SWAP', self.leverage)
            if res_lev and res_lev.get('code') == '0':
                print(f"  杠杆设置成功: {self.leverage}x")
            else:
                print(f"  杠杆设置结果: {res_lev.get('msg') if res_lev else '失败'}")

        except Exception as e:
            print(f"  初始化账户出错: {e}")

    def _normalize_symbol(self, symbol: str) -> str:
        return symbol.replace('/', '-')

    def _eth_to_sz(self, eth_amount: float) -> str:
        """
        将 ETH 数量转换为合约张数 (整数)
        Formula: 张数 = ETH数量 / 面值
        """
        sz = int(round(eth_amount / self.ct_val))
        return str(max(1, sz))

    def submit_order(self, order: Order) -> str:
        """提交合约订单"""
        inst_id = self._normalize_symbol(order.symbol)
        side = order.side.value # 'buy' or 'sell'
        ord_type = order.order_type.value
        
        # 1. 生成客户端订单 ID (clOrdId)，用于追踪策略意图
        cl_ord_id = order.meta.get('clOrdId') or f"v93_{int(time.time()*1000)}"
        order.meta['clOrdId'] = cl_ord_id

        # 合约下单需要 posSide
        pos_side = order.meta.get('posSide')
        if not pos_side:
            pos_side = 'long' if side == 'buy' else 'short'

        # 数量转换：ETH -> 张
        sz = self._eth_to_sz(order.size)
        px = str(order.price) if (order.price and order.order_type == OrderType.LIMIT) else None

        # 2. 强化日志记录下单意图
        reason = order.meta.get('reason', '未知理由')
        level = order.meta.get('level', '-')
        print(f"[下单请求] {reason} | 层级:{level} | {inst_id} {side} {pos_side} sz={sz}张 | clOrdId:{cl_ord_id}")
        
        result = self.api.place_order(
            inst_id=inst_id,
            side=side,
            ord_type=ord_type,
            sz=sz,
            px=px,
            td_mode='cross',
            pos_side=pos_side,
            cl_ord_id=cl_ord_id  # 传递 clOrdId
        )

        if result and result.get('code') == '0':
            ord_id = result['data'][0]['ordId']
            order.order_id = ord_id
            order.status = OrderStatus.SUBMITTED
            self._order_map[ord_id] = order
            print(f"[下单成功] 交易所ID:{ord_id} | clOrdId:{cl_ord_id}")
            
            if ord_type == 'market':
                self._handle_immediate_fill(order, ord_id, sz)
            
            return ord_id

        order.status = OrderStatus.REJECTED
        reason_msg = result.get('msg', 'unknown_error') if result else "request_failed"
        order.meta['reject_reason'] = reason_msg
        print(f"[下单被拒] 理由:{reason_msg} | clOrdId:{cl_ord_id}")
        return ""

    def _handle_immediate_fill(self, order: Order, ord_id: str, sz_str: str):
        """市价单快速成交处理"""
        ticker = self.api.get_ticker(self._normalize_symbol(order.symbol))
        price = float(ticker['last']) if ticker else (order.price or 0.0)
        
        # 实际成交的 ETH 数量 = 张数 * 面值
        filled_eth = int(sz_str) * self.ct_val
        
        fill = FillEvent(
            order_id=ord_id,
            symbol=order.symbol,
            side=order.side,
            filled_size=filled_eth,
            filled_price=price,
            timestamp=datetime.now(timezone.utc),
            quote_amount=filled_eth * price,
            meta={'posSide': order.meta.get('posSide')}
        )
        self._notify_fill(fill)

    def cancel_order(self, order_id: str) -> bool:
        # 合约撤单待完善
        return False

    def get_position(self, symbol: str) -> Optional[Position]:
        positions = self.get_all_positions()
        for p in positions:
            if p.symbol == symbol:
                return p
        return None

    def get_all_positions(self) -> List[Position]:
        """获取所有合约持仓"""
        api_pos = self.api.get_positions(inst_type='SWAP')
        res = []
        for p in api_pos:
            inst_id = p.get('instId')
            # OKX Swap 返回的 pos 是张数
            pos_sz_contracts = float(p.get('pos', 0))
            if abs(pos_sz_contracts) < 1e-8:
                continue
                
            # 转换为 ETH 数量
            # 注意：p.get('posSide') 为 'long' 时，pos 为正；'short' 时，pos 为正（但实际是空头）
            # 我们统一用 size 的正负表示：多头为正，空头为负
            side = p.get('posSide')
            eth_size = pos_sz_contracts * self.ct_val
            if side == 'short':
                eth_size = -abs(eth_size)
            else:
                eth_size = abs(eth_size)
                
            res.append(Position(
                symbol=inst_id,
                size=eth_size,
                avg_price=float(p.get('avgPx', 0)),
                entry_time=datetime.fromtimestamp(int(p.get('cTime', 0))/1000, tz=timezone.utc) if p.get('cTime') else datetime.now(timezone.utc),
                unrealized_pnl=float(p.get('upl', 0))
            ))
        return res

    def get_cash(self) -> float:
        """获取全仓可用保证金 (USDT)"""
        bal = self.api.get_balance('USDT')
        if bal:
            return bal['availBal']
        return 0.0

    def get_total_value(self) -> float:
        """获取账户总价值 (口径: 仅 USDT 权益)"""
        try:
            bal = self.api.get_balance('USDT')
            if bal:
                return bal['eq']
            return 0.0
        except Exception as e:
            print(f"[执行器] 计算总价值失败: {e}")
            return 0.0

    def update_market_data(self, timestamp: datetime, price: float):
        pass

    def set_leverage(self, leverage: float):
        """设置杠杆"""
        self.leverage = leverage
        inst_id = self._normalize_symbol('ETH-USDT-SWAP')
        res = self.api.set_leverage(inst_id, int(leverage))
        if res and res.get('code') == '0':
            print(f"[执行器] 杠杆调整成功: {leverage}x")
        else:
            print(f"[执行器] 杠杆调整失败: {res.get('msg') if res else 'API错误'}")

    def get_recent_fills(self, symbol: str, limit: int = 100) -> List[Dict]:
        """获取并返回原始成交数据"""
        # 注意：这里我们使用统一的 instId 转换
        inst_id = self._normalize_symbol(symbol)
        res = self.api.get_fills(inst_id, limit=limit)
        if res and res.get('code') == '0':
            return res.get('data', [])
        return []

    def get_order_history(self, symbol: str, limit: int = 100) -> List[Dict]:
        """获取并返回历史订单数据"""
        inst_id = self._normalize_symbol(symbol)
        res = self.api.get_order_history(inst_id, limit=limit)
        if res and res.get('code') == '0':
            return res.get('data', [])
        return []

    def get_recent_bills(self, limit: int = 100) -> List[Dict]:
        """获取最近账单 (用于净值曲线，口径: 仅 USDT)"""
        # 强制只拉取 USDT 的账单，避免被 ETH 现货价值干扰历史基准
        res = self.api.get_bills(inst_type='SWAP', ccy='USDT', limit=limit)
        if res and res.get('code') == '0':
            return res.get('data', [])
        return []
