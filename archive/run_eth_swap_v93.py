import eventlet
eventlet.monkey_patch()

import os
import sys
import argparse
from datetime import datetime, timezone

# 关键步骤：确保当前目录在路径中
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

# 从内部模块导入
from strategies.eth_swap_v93 import V93Strategy
from executors.okx_swap import OKXSwapExecutor
from datafeeds.okx_feed import OKXDataFeed
from engines.live import LiveEngine
from dashboard.server import create_dashboard

import dashboard.server as ds_mod
print(f"\n[DEBUG] Dashboard Server Module File: {ds_mod.__file__}")

# 从 ethswap.config 导入（因为 ethswap 已经在 sys.path 中）
from config.api_config import OKX_CONFIG, DEFAULT_SYMBOL

def run_strategy(demo=True):
    print("\n" + "="*60)
    print(f"CTS1 - ETH Swap V9.3 策略容器 (模式: {'模拟盘' if demo else '实盘'})")
    print("="*60)
    
    # 1. 初始化 Dashboard
    print("[1/5] 启动监控面板...")
    dashboard = create_dashboard(port=5090)
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
    
    # 3. 执行器配置 (合约专用)
    print("[3/5] 初始化 OKX 合约执行器...")
    executor = OKXSwapExecutor(
        api_key=OKX_CONFIG['api_key'],
        api_secret=OKX_CONFIG['api_secret'],
        passphrase=OKX_CONFIG['passphrase'],
        is_demo=demo,
        leverage=3
    )
    
    # 4. 数据源配置
    print("[4/5] 启动数据流...")
    data_feed = OKXDataFeed(
        symbol=DEFAULT_SYMBOL,
        api_key=OKX_CONFIG['api_key'],
        api_secret=OKX_CONFIG['api_secret'],
        passphrase=OKX_CONFIG['passphrase'],
        is_demo=demo,
        poll_interval=1.0
    )
    
    # 5. 引擎装配
    print("[5/5] 启动交易引擎...")
    engine = LiveEngine(
        strategy=strategy,
        executor=executor,
        data_feed=data_feed,
        warmup_bars=360
    )
    
    # 注册面板更新
    engine.register_status_callback(dashboard.update)
    
    # 注册重置回调
    manual_reset_flag = {'triggered': False}

    def handle_dashboard_reset():
        print("\n[系统] 响应前端重置请求...")
        manual_reset_flag['triggered'] = True
        dashboard.reset_ui() # 立即清空前端显示
        engine.reset() # 停止并标记重启
        
    dashboard.on_reset_callback = handle_dashboard_reset
    
    print("\n[系统] 启动完成，开始监听行情...")
    
    while True:
        try:
            # 2025-03-25 修复：每次启动前强制重置 UI，确保比例尺和历史数据从零开始，避免收缩变形
            dashboard.reset_ui()
            engine.run()
            
            # 检查是否是因为重置而停止
            if getattr(engine, '_should_restart', False):
                print("\n" + "="*60 + "\n[系统] 正在重新启动策略...\n" + "="*60 + "\n")
                
                # 如果是手动重置，强制策略重算网格
                if manual_reset_flag['triggered']:
                    strategy_params['force_reset_grid'] = True
                    manual_reset_flag['triggered'] = False
                else:
                    strategy_params['force_reset_grid'] = False

                # 重新初始化引擎组件
                # 重新创建策略、执行器和引擎，以确保状态完全清零
                strategy = V93Strategy(**strategy_params)
                # 执行器不需要完全重新创建，但可以重置下杠杆等
                
                engine = LiveEngine(
                    strategy=strategy,
                    executor=executor,
                    data_feed=data_feed,
                    warmup_bars=360
                )
                engine.register_status_callback(dashboard.update)
                dashboard.on_reset_callback = engine.reset
                continue
            else:
                # 正常停止（如用户 Ctrl+C）
                break
                
        except KeyboardInterrupt:
            print("\n[系统] 用户停止。")
            engine.stop()
            break
        except Exception as e:
            print(f"\n[系统] 运行异常: {e}")
            import traceback
            traceback.print_exc()
            engine.stop()
            break

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='V93 ETH Swap Runner')
    parser.add_argument('--live', action='store_true', help='运行实盘 (默认模拟盘)')
    args = parser.parse_args()
    
    is_demo = not args.live
    run_strategy(demo=is_demo)
