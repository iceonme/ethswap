import json
import os
from collections import defaultdict

def analyze_trades():
    data_dir = 'data'
    trades_file = os.path.join(data_dir, 'v95_trades_paper.json')
    initial_balance_file = os.path.join(data_dir, 'v95_initial_balance_paper.json')

    if not os.path.exists(trades_file):
        print("未找到交易记录文件。")
        return

    with open(trades_file, 'r', encoding='utf-8') as f:
        trades = json.load(f)

    with open(initial_balance_file, 'r', encoding='utf-8') as f:
        initial_balance = json.load(f).get('initial_balance', 0.0)

    # Track positions: { (symbol, side): { 'size': 0.0, 'total_cost': 0.0 } }
    positions = defaultdict(lambda: {'size': 0.0, 'cost': 0.0})
    realized_pnl = 0.0
    total_fees = 0.0

    print(f"--- 详细持仓分析 ---")
    for t in trades:
        symbol = t.get('symbol')
        action = t.get('action', '')
        size = float(t.get('size', 0.0))
        price = float(t.get('price', 0.0))
        pnl = float(t.get('pnl', 0.0))
        
        # Fee estimation (0.05%)
        fee = price * size * 0.0005
        total_fees += fee

        pos_side = 'long' if ('多' in action or 'long' in t.get('meta', {}).get('posSide', '')) else 'short'
        key = (symbol, pos_side)

        if '开' in action:
            # Entry: Increase size and update cost
            positions[key]['cost'] = (positions[key]['size'] * positions[key]['cost'] + size * price) / (positions[key]['size'] + size)
            positions[key]['size'] += size
        elif '平' in action:
            # Exit: Decrease size, PnL is usually provided in the record
            # We trust the PnL in the record as it's what the executor saved
            realized_pnl += pnl
            positions[key]['size'] -= size
            if positions[key]['size'] < 1e-8:
                positions[key]['size'] = 0.0
                positions[key]['cost'] = 0.0
        else:
            # Might be other actions (like forced clear)
            pass

    current_balance = initial_balance + realized_pnl - total_fees
    
    print(f"初始余额: {initial_balance:.2f}")
    print(f"累计已实现盈亏: {realized_pnl:.2f}")
    print(f"估算总手续费: {total_fees:.2f}")
    print(f"理论账户余额 (Balance): {current_balance:.2f}")
    
    print("\n当前活跃持仓 (从交易记录还原):")
    any_pos = False
    total_unrealized = 0.0
    for key, data in positions.items():
        if data['size'] > 1e-8:
            any_pos = True
            symbol, side = key
            print(f"- {symbol} {side}: 数量={data['size']:.4f}, 均价={data['cost']:.2f}")
    
    if not any_pos:
        print("- 无活跃持仓")

if __name__ == "__main__":
    analyze_trades()
