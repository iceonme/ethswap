import eventlet
eventlet.monkey_patch()

import os
import sys
from datetime import datetime, timezone

# 确保项目目录在路径中
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

# 导入模块
from strategies.eth_swap_v93 import V93Strategy
from executors.mock_swap import MockSwapExecutor
from datafeeds.okx_feed import OKXDataFeed
from engines.live import LiveEngine
from dashboard.server import create_dashboard

# 从 config 导入实盘配置 (Paper Test 也需要实盘行情数据)
from config.api_config import LIVE_CONFIG, DEFAULT_SYMBOL

def run_paper_trading():
    print("\n" + "="*60)
    print("CTS1 - ETH Swap V9.3 [Paper Test 纸笔交易]")
    print("="*60)
    print("模式: 实盘行情数据 + 本地模拟执行")
    print("初始本金: 10,000.00 USDT")
    print("="*60)
    
    # Paper test 使用实盘行情，如果用户没填密钥，可以用 demo 密钥凑合，
    # 但建议用实盘密钥以获取更稳定的行情数据。
    # 这里我们优先尝试 LIVE_CONFIG，如果未填，则尝试 OKX_CONFIG。
    from config.api_config import OKX_CONFIG
    api_config = LIVE_CONFIG if LIVE_CONFIG['api_key'] != 'YOUR_REAL_API_KEY' else OKX_CONFIG
    
    # 1. 启动 Dashboard (端口 5100)
    port = 5100
    print(f"[1/5] 启动 Paper 监控面板 (Port: {port})...")
    dashboard = create_dashboard(port=port)
    dashboard.start_background()
    
    # 2. 策略配置
    print("[2/5] 初始化 V93 策略...")
    strategy_params = {
        'symbol': DEFAULT_SYMBOL,
        'base_position_eth': 0.05,
        'rsi_period': 14,
        'leverage_base': 3,
    }
    strategy = V93Strategy(**strategy_params)
    
    # 3. 执行器配置 (Mock 模拟)
    print("[3/5] 初始化本地模拟执行器 (10000 USDT)...")
    executor = MockSwapExecutor(initial_cash=10000.0)
    
    # 4. 数据源配置 (实盘行情)
    print(f"[4/5] 启动行情数据流 (使用{'实盘' if api_config == LIVE_CONFIG else '模拟盘'}凭据)...")
    data_feed = OKXDataFeed(
        symbol=DEFAULT_SYMBOL,
        api_key=api_config['api_key'],
        api_secret=api_config['api_secret'],
        passphrase=api_config['passphrase'],
        is_demo=api_config.get('is_demo', False),
        poll_interval=1.0
    )
    
    # 5. 引擎装配 (使用 _paper 后缀进行数据隔离)
    print("[5/5] 启动 Paper 交易引擎 (数据隔离模式)...")
    engine = LiveEngine(
        strategy=strategy,
        executor=executor,
        data_feed=data_feed,
        warmup_bars=360,
        data_suffix="paper"
    )
    
    # 注册面板更新
    engine.register_status_callback(dashboard.update)
    
    # 注册重置回调
    manual_reset_flag = {'triggered': False}

    def handle_dashboard_reset():
        print("\n[系统] 响应 Paper 前端重置请求...")
        manual_reset_flag['triggered'] = True
        dashboard.reset_ui()
        engine.reset()
        
    dashboard.on_reset_callback = handle_dashboard_reset
    
    print("\n[系统] Paper 模式启动完成，行情实时同步中...")
    
    while True:
        try:
            dashboard.reset_ui()
            engine.run()
            
            if getattr(engine, '_should_restart', False):
                print("\n[系统] 正在重新启动 Paper 策略...")
                if manual_reset_flag['triggered']:
                    strategy_params['force_reset_grid'] = True
                    manual_reset_flag['triggered'] = False
                else:
                    strategy_params['force_reset_grid'] = False

                strategy = V93Strategy(**strategy_params)
                engine = LiveEngine(
                    strategy=strategy,
                    executor=executor,
                    data_feed=data_feed,
                    warmup_bars=360,
                    data_suffix="paper"
                )
                engine.register_status_callback(dashboard.update)
                dashboard.on_reset_callback = engine.reset
                continue
            else:
                break
                
        except KeyboardInterrupt:
            print("\n[系统] 退出 Paper 模式。")
            engine.stop()
            break
        except Exception as e:
            print(f"\n[系统] Paper 运行异常: {e}")
            import traceback
            traceback.print_exc()
            engine.stop()
            break

if __name__ == "__main__":
    run_paper_trading()
