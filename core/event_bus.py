"""
轻量级内存事件总线 (EventBus)
"""

from typing import Callable, Dict, List, Any
import logging

logger = logging.getLogger(__name__)

class EventBus:
    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = {}

    def subscribe(self, event_type: str, callback: Callable):
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(callback)
        # logger.debug(f"Subscribed to {event_type}: {callback.__name__}")

    def publish(self, event_type: str, data: Any):
        if event_type in self._subscribers:
            for callback in self._subscribers[event_type]:
                try:
                    callback(data)
                except Exception as e:
                    logger.error(f"Error in subscriber {callback.__name__} for {event_type}: {e}")
                    import traceback
                    logger.error(traceback.format_exc())

# 全局单例 (也可以注入，但在极简架构中单例更方便)
bus = EventBus()
