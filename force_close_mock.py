import eventlet
eventlet.monkey_patch()
import os
import sys

# 确保项目目录在路径中
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from executors.mock_swap import MockSwapExecutor
from config.api_config import DEFAULT_SYMBOL

def main():
    print("="*50)
    print("    清理虚拟执行器 (MockSwap) 所有本地持仓")
    print("="*50)
    
    executor = MockSwapExecutor()
    positions = executor.get_position().get('data', [])
    
    if not positions:
         print("\n[INFO] 当前没有探测到任何本地虚拟持仓。")
         return

    print(f"\n[INFO] 检测到 {len(positions)} 个持仓，准备全部平仓：")
    for p in positions:
        print(f"  - 标的: {p.get('instId')} | 数量: {p.get('pos')} | 均价: {p.get('avgPx')}")
        
    # 我们知道在 MockSwap 中 close_position 会清理当前 symbol
    # 但模拟执行器实际上使用缓存的数据文件存储状态，我们需要调用方法来重置
    print("\n[ACTION] 正在发送平仓指令...")
    
    result = executor.close_position(instId=DEFAULT_SYMBOL)
    
    if result.get('code') == '0':
         print("\n[SUCCESS] 平仓成功！当前所有的多头和空头都已被强制清理。")
         print("您现在可以重新启动 run_ethswap_paper.py，策略将以全新的单向状态启动！")
    else:
         print(f"\n[ERROR] 平仓失败: {result.get('msg')}")
         # 尝试粗暴地删除本地状态文件以防万一
         try:
              state_file = os.path.join(CURRENT_DIR, 'data', 'mock_account.json')
              if os.path.exists(state_file):
                  os.remove(state_file)
                  print("[WARN] 已直接移除本地模拟账户状态文件，请重启引擎恢复初始 10000 U。")
         except Exception as e:
              pass

if __name__ == "__main__":
    main()
