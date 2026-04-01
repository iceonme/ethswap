"""
CTS1 核心数据类型
所有模块共享的基础数据结构
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Dict, List, Optional, Any
import pandas as pd


class Side(Enum):
    """交易方向"""
    BUY = "buy"
    SELL = "sell"
    
    def opposite(self) -> "Side":
        return Side.SELL if self == Side.BUY else Side.BUY


class OrderType(Enum):
    """订单类型"""
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class OrderStatus(Enum):
    """订单状态"""
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIAL_FILLED = "partial_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class MarketRegime(Enum):
    """市场状态"""
    TRENDING_UP = "上涨趋势"
    TRENDING_DOWN = "下跌趋势"
    RANGING = "震荡区间"
    UNKNOWN = "未知"


@dataclass
class Signal:
    """
    策略输出的交易信号
    策略只负责输出信号，不关心如何执行
    """
    timestamp: datetime
    symbol: str
    side: Side
    size: float           # 建议数量
    price: Optional[float] = None  # 目标价格（None表示市价）
    order_type: OrderType = OrderType.MARKET
    confidence: float = 1.0        # 信号强度 0-1
    reason: str = ""               # 信号原因说明
    meta: Dict[str, Any] = field(default_factory=dict)  # 额外信息
    
    def __post_init__(self):
        if self.confidence < 0 or self.confidence > 1:
            raise ValueError("confidence must be in [0, 1]")


@dataclass  
class Order:
    """
    发送到执行器的订单
    """
    order_id: str
    symbol: str
    side: Side
    size: float
    order_type: OrderType
    price: Optional[float] = None
    timestamp: Optional[datetime] = None
    status: OrderStatus = OrderStatus.PENDING
    filled_size: float = 0.0
    avg_price: float = 0.0
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FillEvent:
    """
    成交回报事件
    执行器通知策略成交结果
    """
    order_id: str
    symbol: str
    side: Side
    filled_size: float
    filled_price: float
    timestamp: datetime
    fee: float = 0.0
    pnl: Optional[float] = None  # 平仓时才有
    quote_amount: Optional[float] = None  # 报价币种金额（如USDT金额）
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Position:
    """
    持仓信息（支持双向）
    """
    symbol: str
    size: float           # 正数=多头，负数=空头
    avg_price: float      # 平均成本价
    entry_time: datetime
    unrealized_pnl: float = 0.0
    
    @property
    def is_long(self) -> bool:
        return self.size > 0

    @property
    def is_short(self) -> bool:
        return self.size < 0
    
    @property
    def market_value(self, current_price: float) -> float:
        return abs(self.size) * current_price


@dataclass
class MarketData:
    """
    市场数据（单根K线或Tick）
    """
    timestamp: datetime
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    
    @classmethod
    def from_series(cls, timestamp: datetime, symbol: str, row: pd.Series) -> "MarketData":
        """从pandas Series创建"""
        return cls(
            timestamp=timestamp,
            symbol=symbol,
            open=float(row['open']),
            high=float(row['high']),
            low=float(row['low']),
            close=float(row['close']),
            volume=float(row.get('volume', 0))
        )


@dataclass
class TradeRecord:
    """
    交易记录（用于回测报告/Dashboard）
    """
    timestamp: datetime
    symbol: str
    side: Side
    size: float
    price: float
    fee: float
    pnl: Optional[float] = None
    reason: str = ""


@dataclass
class PortfolioSnapshot:
    """
    投资组合快照
    """
    timestamp: datetime
    cash: float
    positions: Dict[str, Position]
    total_value: float
    
    @property
    def position_value(self) -> float:
        return self.total_value - self.cash


@dataclass
class StrategyContext:
    """
    策略运行上下文
    引擎提供给策略的当前状态信息
    """
    timestamp: datetime
    cash: float
    positions: Dict[str, Position]  # symbol -> Position
    current_prices: Dict[str, float]  # symbol -> price
    meta: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def total_value(self) -> float:
        """总资产价值"""
        position_value = sum(
            pos.size * self.current_prices.get(pos.symbol, pos.avg_price)
            for symbol, pos in self.positions.items()
        )
        return self.cash + position_value
