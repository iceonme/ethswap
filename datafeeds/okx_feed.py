"""
OKX 实时数据接入
"""
import time
from datetime import datetime
from typing import Iterator, Optional

from core.types import MarketData
from .base import BaseDataFeed
from config.okx_config import OKXAPI

class OKXDataFeed(BaseDataFeed):
    def __init__(self, 
                 symbol: str = "ETH-USDT-SWAP",
                 timeframe: str = "1m",
                 api: Optional[OKXAPI] = None,
                 api_key: Optional[str] = None,
                 api_secret: Optional[str] = None,
                 passphrase: Optional[str] = None,
                 is_demo: bool = True,
                 poll_interval: float = 2.0):
        super().__init__([symbol])
        self.symbol = symbol
        self.timeframe = timeframe
        self.poll_interval = poll_interval
        
        if api:
            self.api = api
        else:
            self.api = OKXAPI(
                api_key=api_key,
                api_secret=api_secret,
                passphrase=passphrase,
                is_demo=is_demo
            )
        
        self._inst_id = symbol.replace('/', '-')
        self._bar_map = {'1m': '1m', '5m': '5m', '15m': '15m', 
                         '1h': '1H', '4h': '4H', '1d': '1D'}
        self._last_ts = 0 # 记录最后一次推送的时间戳 (ms)
        
    def stream(self,
               start: Optional[datetime] = None,
               end: Optional[datetime] = None) -> Iterator[MarketData]:
        self._running = True
        bar = self._bar_map.get(self.timeframe, '1m')
        
        print(f"启动 OKX 数据流: {self.symbol} {self.timeframe}")
        
        while self._running:
            try:
                # 获取最近 2 根 K 线即可满足盘中实时跳动 (limit=360 仅用于预热)
                df = self.api.get_candles(self._inst_id, bar, limit=2)
                
                if df is not None and len(df) > 0:
                    current = df.iloc[-1]
                    timestamp = df.index[-1]
                    ts_ms = int(timestamp.timestamp() * 1000)
                    
                    # 2025-03-23 优化：移除严格时间戳去重，允许盘中实时跳动
                    self._last_ts = ts_ms
                    
                    data = MarketData(
                        timestamp=timestamp,
                        symbol=self.symbol,
                        open=float(current['open']),
                        high=float(current['high']),
                        low=float(current['low']),
                        close=float(current['close']),
                        volume=float(current['volume'])
                    )
                    
                    self._notify_data(data)
                    # print(f"[数据源] 轮询成功: {data.close:.2f}")
                    yield data
                
                time.sleep(self.poll_interval)
                
            except Exception as e:
                print(f"数据流错误: {e}")
                time.sleep(5)
