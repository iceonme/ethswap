"""
交易数据仓库 (Trade Repository)
"""

import json
import os
from typing import List, Dict
from core.dto import TradeRecord

class JSONTradeRepository:
    def __init__(self, filepath: str):
        self.filepath = filepath

    def save_all(self, trades: List[TradeRecord]):
        try:
            # 确保目录存在
            os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
            data = [t.to_dict() for t in trades]
            with open(self.filepath, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"[TradeRepo] 保存失败: {e}")

    def load_all(self) -> List[TradeRecord]:
        if not os.path.exists(self.filepath):
            return []
        try:
            with open(self.filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return [TradeRecord(**item) for item in data]
        except Exception as e:
            print(f"[TradeRepo] 加载失败: {e}")
            return []
