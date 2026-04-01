import eventlet
eventlet.monkey_patch()

import os
import sys
import argparse
from datetime import datetime, timezone

# 确保项目目录在路径中
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

# 导入模块
from strategies.eth_swap_v93 import V93Strategy
from executors.okx_swap import OKXSwapExecutor
from datafeeds.okx_feed import OKXDataFeed
from engines.live import LiveEngine
from dashboard.server import create_dashboard

# 从 config 导入实盘配置
from config.api_config import LIVE_CONFIG, DEFAULT_SYMBOL

def run_real_trading():
    print("\n" + "="*60)
    print("CTS1 - ETH Swap V9.3 [实盘交易程序]")
    print("="*60)
    
    # 验证配置
    if LIVE_CONFIG['api_key'] == 'YOUR_REAL_API_KEY':
        print("[错误] 请先在 config/api_config.py 中配置 LIVE_CONFIG 的 API 密钥！")
        return

    # 1. 启动 Dashboard (端口 5101)
    port = 5101
    print(f"[1/5] 启动实盘监控面板 (Port: {port})...")
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
    
    # 3. 执行器配置 (合约实盘)
    print("[3/5] 初始化 OKX 实盘合约执行器...")
    executor = OKXSwapExecutor(
        api_key=LIVE_CONFIG['api_key'],
        api_secret=LIVE_CONFIG['api_secret'],
        passphrase=LIVE_CONFIG['passphrase'],
        is_demo=False,
        leverage=3
    )
    
    # 打印初始余额作为安全检查
    initial_val = executor.get_total_value()
    print(f"  >>> 当前实盘账户权益: {initial_val:.2f} USDT")
    
    # 4. 数据源配置 (实盘行情)
    print("[4/5] 启动实盘数据流...")
    data_feed = OKXDataFeed(
        symbol=DEFAULT_SYMBOL,
        api_key=LIVE_CONFIG['api_key'],
        api_secret=LIVE_CONFIG['api_secret'],
        passphrase=LIVE_CONFIG['passphrase'],
        is_demo=False,
        poll_interval=1.0
    )
    
    # 5. 引擎装配 (使用 _real 后缀进行数据隔离)
    print("[5/5] 启动实盘交易引擎 (数据隔离模式)...")
    engine = LiveEngine(
        strategy=strategy,
        executor=executor,
        data_feed=data_feed,
        warmup_bars=360,
        data_suffix="real"
    )
    
    # 注册面板更新
    engine.register_status_callback(dashboard.update)
    
    # 注册重置回调
    manual_reset_flag = {'triggered': False}

    def handle_dashboard_reset():
        print("\n[系统] 响应实盘前端重置请求...")
        manual_reset_flag['triggered'] = True
        dashboard.reset_ui()
        engine.reset()
        
    dashboard.on_reset_callback = handle_dashboard_reset
    
    print("\n[系统] 实盘启动完成，开始监听行情...")
    
    while True:
        try:
            dashboard.reset_ui()
            engine.run()
            
            if getattr(engine, '_should_restart', False):
                print("\n[系统] 正在重新启动实盘策略...")
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
                    data_suffix="real"
                )
                engine.register_status_callback(dashboard.update)
                dashboard.on_reset_callback = engine.reset
                continue
            else:
                break
                
        except KeyboardInterrupt:
            print("\n[系统] 用户手动停止实盘。")
            engine.stop()
            break
        except Exception as e:
            print(f"\n[系统] 实盘运行异常: {e}")
            import traceback
            traceback.print_exc()
            engine.stop()
            break

if __name__ == "__main__":
    run_real_trading()
