"""
执行器基类
"""
from abc import ABC, abstractmethod
from typing import Optional, List, Callable
from datetime import datetime

from core.types import Order, FillEvent, Position, OrderStatus

class BaseExecutor(ABC):
    def __init__(self):
        self._fill_callbacks: List[Callable[[FillEvent], None]] = []
        
    def register_fill_callback(self, callback: Callable[[FillEvent], None]):
        if callback not in self._fill_callbacks:
            self._fill_callbacks.append(callback)
            
    def clear_fill_callbacks(self):
        self._fill_callbacks = []
    
    def _notify_fill(self, fill: FillEvent):
        for callback in self._fill_callbacks:
            callback(fill)
    
    @abstractmethod
    def submit_order(self, order: Order) -> str:
        pass
    
    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        pass
    
    @abstractmethod
    def get_position(self, symbol: str) -> Optional[Position]:
        pass
    
    @abstractmethod
    def get_all_positions(self) -> List[Position]:
        pass
    
    @abstractmethod
    def get_cash(self) -> float:
        pass
    
    def get_order_status(self, order_id: str) -> Optional[OrderStatus]:
        return None
    
    def update_market_data(self, timestamp: datetime, price: float):
        pass

    def set_leverage(self, leverage: float):
        """设置杠杆（可选实现）"""
        pass
