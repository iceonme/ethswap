import os
import sys
import logging
from datetime import datetime, timezone

# 确保项目目录在路径中
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

# 配置日志格式 (全系统统一)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(CURRENT_DIR, 'logs', 'system.log'), encoding='utf-8')
    ]
)
logger = logging.getLogger('Boot')

# 确保必要的目录存在
os.makedirs(os.path.join(CURRENT_DIR, 'logs'), exist_ok=True)
os.makedirs(os.path.join(CURRENT_DIR, 'data'), exist_ok=True)

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
    port = 5100
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
    
    # 3. 执行器配置 (Mock 模拟)
    print("[3/5] 初始化本地模拟执行器 (10000 USDT)...")
    executor = OKXPaperExecutor(initial_cash=10000.0)
    
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
