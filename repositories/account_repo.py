"""
账户与重置历史仓库 (Account & Reset Repository)
"""

import json
import os
from typing import List, Optional, Dict
from core.dto import ResetEvent

class JSONAccountRepository:
    def __init__(self, balance_file: str, reset_file: str):
        self.balance_file = balance_file
        self.reset_file = reset_file

    def save_initial_balance(self, value: float):
        try:
            os.makedirs(os.path.dirname(self.balance_file), exist_ok=True)
            with open(self.balance_file, 'w') as f:
                json.dump({'initial_balance': value}, f)
        except Exception as e:
            print(f"[AccountRepo] 保存余额失败: {e}")

    def load_initial_balance(self) -> Optional[float]:
        if not os.path.exists(self.balance_file):
            return None
        try:
            with open(self.balance_file, 'r') as f:
                data = json.load(f)
            return data.get('initial_balance')
        except Exception as e:
            print(f"[AccountRepo] 加载余额失败: {e}")
            return None

    def save_reset_event(self, event: ResetEvent):
        history = self.load_reset_history()
        history.append(event)
        try:
            os.makedirs(os.path.dirname(self.reset_file), exist_ok=True)
            data = [e.__dict__ for e in history]
            with open(self.reset_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"[AccountRepo] 保存重置事件失败: {e}")

    def load_reset_history(self) -> List[ResetEvent]:
        if not os.path.exists(self.reset_file):
            return []
        try:
            with open(self.reset_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return [ResetEvent(**item) for item in data]
        except Exception as e:
            print(f"[AccountRepo] 加载重置历史失败: {e}")
            return []
