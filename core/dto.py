"""
领域数据传输对象 (DTO)
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, Any

@dataclass
class TradeRecord:
    type: str # BUY / SELL
    action: str # 开多 / 平多 / ...
    symbol: str
    price: float
    size: float
    quote_amount: float
    t: int # timestamp ms
    time: str # ISO format
    reason: str
    detail: str
    pnl: Optional[float] = None
    margin: Optional[float] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return self.__dict__

@dataclass
class AccountSnapshot:
    initial_balance: float
    current_balance: float
    equity: float
    timestamp: int

@dataclass
class ResetEvent:
    t: int
    time: str
    equity: float
    reason: str

@dataclass
class CandleEvent:
    symbol: str
    data: Any # MarketData
    equity: float
    rsi: float

@dataclass
class FillEventPayload:
    fill: Any # core.types.FillEvent
