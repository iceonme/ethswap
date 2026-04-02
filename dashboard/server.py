import threading
import json
import logging
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from flask import Flask, render_template, jsonify, make_response
from flask_socketio import SocketIO, emit

logger = logging.getLogger('Dashboard')

class DashboardServer:
    """
    Dashboard 服务器
    
    功能：
    1. 接收引擎状态更新
    2. 通过 WebSocket 推送到前端
    3. 提供 REST API
    """
    
    def __init__(self, host='0.0.0.0', port=5000):
        self.host = host
        self.port = port
        
        # Flask 应用
        import os
        base_dir = os.path.dirname(os.path.abspath(__file__))
        template_dir = os.path.join(base_dir, 'templates')
        static_dir = os.path.join(base_dir, 'static')
        self.app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)
        self.app.config['SECRET_KEY'] = 'cts1-secret-key'
        self.app.config['TEMPLATES_AUTO_RELOAD'] = True  # 开启模板自动重载
        
        # SocketIO (强制使用 threading 模式避开 eventlet 在 Windows 下的兼容性问题)
        self.socketio = SocketIO(self.app, 
                                 cors_allowed_origins="*", 
                                 async_mode='threading', # Windows下线程模式最稳定
                                 ping_timeout=10,
                                 ping_interval=5,
                                 logger=False,           # 禁用内部日志，提高 Werkzeug 3.1 兼容性
                                 engineio_logger=False)
        
        # 数据缓存
        self._data: Dict[str, Any] = {
            'prices': {},
            'total_value': 0,
            'cash': 0,
            'position_value': 0,
            'positions': {},
            'pnl_pct': 0,
            'rsi': 50,
            'trade_history': [],
            'history_candles': [],
            'history_rsi': [],
            'history_equity': [],
            'strategy': {}
        }
        
        self._setup_routes()
        self._setup_socketio()
    
    def _setup_routes(self):
        """设置路由"""
        
        # 版本号 - 每次修改前端代码后更新
        self.version = "v4.0-Innovation-v1.12"
        # 获取 template_dir 的逻辑在 __init__ 中，这里我们重新获取一下用于调试
        import os
        td = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')
        print(f"[DEBUG] Dashboard Template Path: {td}")
        
        @self.app.route('/')
        def index():
            timestamp = datetime.now().strftime('%Y%m%d%H%M%S%f')
            res = make_response(render_template('dashboard.html', 
                                                  version=timestamp,
                                                  app_version=self.version))
            res.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0, private'
            res.headers['Pragma'] = 'no-cache'
            res.headers['Expires'] = '-1'
            res.headers['Vary'] = '*'
            return res
        
        @self.app.route('/api/status')
        def api_status():
            return jsonify(self._clean_data(self._data))
        
        @self.app.route('/favicon.ico')
        def favicon():
            return '', 204
            
        @self.app.errorhandler(404)
        def handle_404(e):
            """显式 404 处理器，防止 Werkzeug 3.x 默认错误响应导致 write() 冲突"""
            return jsonify(error="Not Found"), 404
    
    def _setup_socketio(self):
        """设置 WebSocket 事件"""
        
        @self.socketio.on('connect')
        def handle_connect():
            from datetime import timezone
            print('[SocketIO] 客户端已连接')
            print(f"[SocketIO] 当前历史数据: {len(self._data.get('history_candles', []))} 根 K 线")
            emit('server_ready', {
                'status': 'active',
                'time': datetime.now(timezone.utc).isoformat()
            })
            # 发送全量数据
            clean_data = self._clean_data(self._data)
            print(f"[SocketIO] 发送 update: {len(clean_data.get('history_candles', []))} 根 K 线")
            emit('update', clean_data)
        
        @self.socketio.on('ping')
        def handle_ping():
            from datetime import timezone
            emit('pong', {'time': datetime.now(timezone.utc).isoformat()})
            
        @self.socketio.on('reset_strategy')
        def handle_reset_strategy():
            print("\n[SocketIO] >>> 收到前端重置策略与资金请求 <<<")
            if hasattr(self, 'on_reset_callback') and self.on_reset_callback:
                print("[SocketIO] 执行重置回调函数...")
                self.on_reset_callback()
            else:
                print("[SocketIO] 警告: 未注册重置回调函数")
    
    def _clean_data(self, data: Any, depth=0) -> Any:
        """清理数据，确保可序列化，防止递归深度过大"""
        import math
        from enum import Enum
        
        # 防潮：限制递归深度，防止死循环
        if depth > 20:
            return str(data)
            
        try:
            if isinstance(data, dict):
                return {k: self._clean_data(v, depth + 1) for k, v in data.items()}
            elif isinstance(data, list):
                return [self._clean_data(v, depth + 1) for v in data]
            elif isinstance(data, (float, int)):
                if math.isnan(data) or math.isinf(data):
                    return None
                return data
            # 处理 numpy 类型
            elif hasattr(data, 'item') and hasattr(data, 'dtype'):
                return data.item()
            elif isinstance(data, datetime):
                return data.isoformat()
            elif isinstance(data, Enum):
                return data.value
            return data
        except Exception:
            return str(data)
    
    def update(self, data: Dict[str, Any]):
        """更新数据并推送到前端"""
        try:
            # 2025-03-23 优化：支持全量历史同步和增量实时更新
            # 1. K 线处理
            if 'history_candles' in data:
                # 优先使用全量同步数据
                self._data['history_candles'] = data['history_candles'][-1000:]
            elif 'candle' in data:
                # 增量实时更新
                c = data['candle']
                hist = self._data.get('history_candles', [])
                if hist and hist[-1]['t'] == c['t']:
                    hist[-1] = c
                else:
                    hist.append(c)
                self._data['history_candles'] = hist[-1000:]
            
            # 2. RSI 处理
            if 'history_rsi' in data:
                self._data['history_rsi'] = data['history_rsi'][-1000:]
            elif 'rsi' in data and 'candle' in data:
                r = float(data['rsi'])
                hist_rsi = self._data.get('history_rsi', [])
                # 简单对齐：如果 K 线增加，RSI 也增加
                if len(self._data.get('history_candles', [])) > len(hist_rsi):
                    hist_rsi.append(r)
                elif hist_rsi:
                    hist_rsi[-1] = r
                self._data['history_rsi'] = hist_rsi[-1000:]
            
            # 3. 资产历史处理
            if 'history_equity' in data:
                self._data['history_equity'] = data['history_equity'][-1000:]
            elif 'total_value' in data and 'candle' in data:
                v = float(data['total_value'])
                t = data['candle']['t']
                hist_eq = self._data.get('history_equity', [])
                if hist_eq and hist_eq[-1]['t'] == t:
                    hist_eq[-1]['v'] = v
                else:
                    hist_eq.append({'t': t, 'v': v})
                self._data['history_equity'] = hist_eq[-1000:]

            # 合并其余数据 (例如 prices, positions, trades 等)
            for key, value in data.items():
                # 排除已特殊处理的历史列表
                if key in ['history_candles', 'history_rsi', 'history_equity', 'trade_history']:
                    continue

                # 2026-03-28 修复：positions 应该全量覆盖，而不是 update。
                # update({}) 无法清除旧仓位，导致前端产生“僵死仓位”显示 Bug。
                if key == 'positions':
                    self._data[key] = value
                    continue

                if isinstance(value, dict) and key in self._data:
                    self._data[key].update(value)
                else:
                    self._data[key] = value
            
            # 特殊处理成交记录 (映射到 trade_history)
            if 'trade_history' in data:
                # 全量同步
                self._data['trade_history'] = data['trade_history'][-500:]
            elif 'trade' in data:
                # 增量实时成交
                t = data['trade']
                trades = self._data.get('trade_history', [])
                if isinstance(trades, list):
                    # 避免重复 (通过 ord_id)
                    ord_id = t.get('meta', {}).get('ord_id')
                    if not any(d.get('meta', {}).get('ord_id') == ord_id for d in trades):
                        trades.append(t)
                        self._data['trade_history'] = trades[-500:]
            
            # 特殊处理初始资金
            if 'initial_balance' in data:
                self._data['initial_balance'] = data['initial_balance']
            
            # 限制交易历史长度
            if 'trade_history' in self._data and isinstance(self._data['trade_history'], list):
                self._data['trade_history'] = self._data['trade_history'][-500:]
            
            # 推送
            clean = self._clean_data(data)
            self.socketio.emit('update', clean, namespace='/')
            
        except Exception as e:
            print(f"[Dashboard] 更新失败: {e}")
            import traceback
            traceback.print_exc()
            
    def reset_ui(self):
        """通知前端清空所有 UI 数据"""
        try:
            # 清空缓存
            self._data = {
                'history_candles': [],
                'history_rsi': [],
                'history_equity': [],
                'trades': [],
                'prices': {},
                'positions': {}
            }
            self.socketio.emit('reset_ui', {}, namespace='/')
            print("[DashboardServer] 已向前端发送 reset_ui 信号")
        except Exception as e:
            print(f"[Dashboard] 发送 reset_ui 失败: {e}")
    
    def start(self, debug=False):
        """启动服务器"""
        print(f"\n{'='*60}")
        print(f"Dashboard 启动")
        print(f"访问地址: http://localhost:{self.port}")
        print(f"{'='*60}\n")
        
        self.socketio.run(
            self.app,
            host=self.host,
            port=self.port,
            debug=debug,
            allow_unsafe_werkzeug=True
        )
    
    def start_background(self):
        """在后台线程启动"""
        thread = threading.Thread(target=self.start, kwargs={'debug': False})
        thread.daemon = True
        thread.start()
        return thread


def create_dashboard(host='0.0.0.0', port=5000) -> DashboardServer:
    """创建 Dashboard 实例"""
    return DashboardServer(host=host, port=port)


# 全局实例（方便导入）
_default_dashboard: Optional[DashboardServer] = None

def get_dashboard() -> Optional[DashboardServer]:
    """获取默认 Dashboard 实例"""
    return _default_dashboard

def set_dashboard(dashboard: DashboardServer):
    """设置默认 Dashboard 实例"""
    global _default_dashboard
    _default_dashboard = dashboard



