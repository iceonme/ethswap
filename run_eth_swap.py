import eventlet
eventlet.monkey_patch()

import os
import sys
import json
import logging
import logging.handlers
from datetime import datetime, timezone

# 确保项目目录在路径中
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

# 确保必要的目录存在
os.makedirs(os.path.join(CURRENT_DIR, 'logs'), exist_ok=True)
os.makedirs(os.path.join(CURRENT_DIR, 'data'), exist_ok=True)

# 1. 定义事件过滤器 (提取核心交易信号)
class EventFilter(logging.Filter):
    def filter(self, record):
        # 关键词：拦截、突破、平仓、入场、信号、成交、同步、初始化、到达、重置
        keywords = ["拦截", "突破", "平仓", "入场", "信号", "成交", "同步", "初始化", "到达", "重置"]
        message = record.getMessage()
        
        # 2025-04-03 优化：屏蔽掉原始 JSON 格式的消息内容 (以 { 或 [ 开头)
        # 防止 SocketIO 的数据包由于包含关键词而被错误抓取到 event.log
        if message.strip().startswith(("{", "[")):
            return False
            
        return any(kw in message for kw in keywords)

# 2. 配置全系统日志架构
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

# 清理已存在的 Handler
for h in root_logger.handlers[:]:
    root_logger.removeHandler(h)

formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s', datefmt='%H:%M:%S')

# A. 控制台输出
ch = logging.StreamHandler(sys.stdout)
ch.setFormatter(formatter)
root_logger.addHandler(ch)

# B. 系统全量日志 (每 1 小时轮转一次，保留最近 6 小时)
system_handler = logging.handlers.TimedRotatingFileHandler(
    os.path.join(CURRENT_DIR, 'logs', 'system.log'),
    when='H', interval=1, backupCount=6, encoding='utf-8'
)
system_handler.setFormatter(formatter)
root_logger.addHandler(system_handler)

# C. 核心事件日志 (筛选重要交易动作，永续追加或按需手动清理)
event_handler = logging.FileHandler(os.path.join(CURRENT_DIR, 'logs', 'event.log'), encoding='utf-8')
event_handler.setFormatter(formatter)
event_handler.addFilter(EventFilter())
root_logger.addHandler(event_handler)
logger = logging.getLogger('Boot')


def _load_v95_paper_snapshot():
    data_dir = os.path.join(CURRENT_DIR, 'data')
    state_path = os.path.join(data_dir, 'v95_state.json')
    balance_path = os.path.join(data_dir, 'v95_initial_balance_paper.json')
    snapshot = {}
    if os.path.exists(state_path):
        try:
            with open(state_path, 'r', encoding='utf-8') as f:
                snapshot = json.load(f) or {}
        except Exception as e:
            logger.warning(f'?? v95_state.json ??: {e}')
    initial_cash = 10000.0
    if snapshot.get('cash') is not None:
        initial_cash = float(snapshot.get('cash'))
    elif snapshot.get('equity'):
        initial_cash = float(snapshot.get('equity'))
    elif os.path.exists(balance_path):
        try:
            with open(balance_path, 'r', encoding='utf-8') as f:
                initial_cash = float((json.load(f) or {}).get('initial_balance', initial_cash))
        except Exception as e:
            logger.warning(f'??????????: {e}')
    return initial_cash, snapshot

# 导入模块
from strategies.eth_swap_v95 import V95Strategy
from executors.okx_paper import OKXPaperExecutor
from datafeeds.okx_feed import OKXDataFeed
from engines.live import LiveEngine
from dashboard.server import create_dashboard

# 从 config 导入实盘配置 (Paper Test 也需要实盘行情数据)
from config.api_config import LIVE_CONFIG, DEFAULT_SYMBOL

def run_paper_trading():
    print("\n" + "="*60)
    print("CTS1 - ETH Swap V9.3 [Paper Test 纸笔交易 - 模块化版]")
    print("="*60)
    print("架构: 实盘行情 + 模拟执行 (模块化分层)")
    print("="*60)
    
    from config.api_config import OKX_CONFIG
    api_config = LIVE_CONFIG if LIVE_CONFIG['api_key'] != 'YOUR_REAL_API_KEY' else OKX_CONFIG
    
    # 1. 启动 Dashboard
    port = 5500
    print(f"[1/5] 启动监控面板 (Port: {port})...")
    dashboard = create_dashboard(port=port)
    dashboard.start_background()
    
    # 2. 策略配置
    print("[2/5] 初始化 V95 策略逻辑...")
    strategy_params = {
        'symbol': DEFAULT_SYMBOL,
        'base_position_eth': 0.05,
        'rsi_period': 14,
        'leverage_base': 3,
        'standalone': False # 关闭策略内部 IO
    }
    strategy = V95Strategy(strategy_params)
    startup_cash, state_snapshot = _load_v95_paper_snapshot()
    
    # 3. 执行器配置 (Mock 模拟)
    print(f"[3/5] 初始化本地模拟执行器 ({startup_cash:.2f} USDT)...")
    executor = OKXPaperExecutor(initial_cash=startup_cash)
    
    # 4. 数据源配置 (实盘行情)
    print(f"[4/5] 启动行情数据流 (使用统一客户端)...")
    data_feed = OKXDataFeed(
        symbol=DEFAULT_SYMBOL,
        api_key=api_config['api_key'],
        api_secret=api_config['api_secret'],
        passphrase=api_config['passphrase'],
        is_demo=api_config.get('is_demo', False)
    )
    
    # 5. 引擎装配
    print("[5/5] 组装实盘引擎 (Paper 隔离模式)...")
    engine = LiveEngine(
        strategy=strategy,
        executor=executor,
        data_feed=data_feed,
        warmup_bars=360,
        data_suffix="paper"
    )
    engine.state_snapshot = state_snapshot
    
    # 注册回调与面板同步
    engine.register_status_callback(dashboard.update)
    dashboard.on_reset_callback = engine.reset
    
    print("\n[系统] Paper 模式组装完成，运行中...")
    
    try:
        engine.run()
    except KeyboardInterrupt:
        print("\n[系统] 用户停止。")
    except Exception as e:
        print(f"\n[系统] 运行异常: {e}")
        import traceback; traceback.print_exc()
    finally:
        engine.stop()

if __name__ == "__main__":
    run_paper_trading()
