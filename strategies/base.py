"""
策略基类
"""
from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any
from datetime import datetime

from core.types import Signal, FillEvent, MarketData, Position, StrategyContext

class BaseStrategy(ABC):
    def __init__(self, name: str = "unnamed", **params):
        self.name = name
        self.params = params
        self._initialized = False
        
    def initialize(self):
        self._initialized = True
    
    @abstractmethod
    def on_data(self, data: MarketData, context: StrategyContext) -> List[Signal]:
        pass
    
    def on_fill(self, fill: FillEvent):
        pass
    
    def on_start(self):
        pass
    
    def on_stop(self):
        pass
    
    def get_param(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)
