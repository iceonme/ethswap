"""
数据接口基类
"""
from abc import ABC, abstractmethod
from typing import Iterator, List, Optional, Callable
from datetime import datetime

from core.types import MarketData

class BaseDataFeed(ABC):
    def __init__(self, symbols: List[str]):
        self.symbols = symbols if isinstance(symbols, list) else [symbols]
        self._data_callbacks: List[Callable[[MarketData], None]] = []
        self._running = False
        
    def register_data_callback(self, callback: Callable[[MarketData], None]):
        self._data_callbacks.append(callback)
    
    def _notify_data(self, data: MarketData):
        for callback in self._data_callbacks:
            callback(data)
    
    @abstractmethod
    def stream(self, start: Optional[datetime] = None, 
               end: Optional[datetime] = None) -> Iterator[MarketData]:
        pass
    
    def get_historical_data(self, start: datetime, end: datetime) -> List[MarketData]:
        return list(self.stream(start, end))
    
    def stop(self):
        self._running = False
