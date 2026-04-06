import json
import os

def check_consistency():
    data_dir = 'data'
    trades_file = os.path.join(data_dir, 'v95_trades_paper.json')
    state_file = os.path.join(data_dir, 'v95_state.json')
    initial_balance_file = os.path.join(data_dir, 'v95_initial_balance_paper.json')

    print(f"--- 诊断报告 ---")

    # 1. Load Initial Balance
    initial_balance = 0.0
    if os.path.exists(initial_balance_file):
        with open(initial_balance_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            initial_balance = data.get('initial_balance', 0.0)
    print(f"初始资金: {initial_balance}")

    # 2. Load Trades and calculate realized PnL
    realized_pnl = 0.0
    trade_count = 0
    fees = 0.0
    if os.path.exists(trades_file):
        with open(trades_file, 'r', encoding='utf-8') as f:
            trades = json.load(f)
            trade_count = len(trades)
            for t in trades:
                pnl = t.get('pnl', 0.0)
                if pnl is None: pnl = 0.0
                realized_pnl += pnl
                
                # Approximate fees for '开' (entry) trades if not already included in PnL
                # Usually in this system, pnl is only for exit trades.
                # Entries have fees too.
                action = t.get('action', '')
                if '开' in action:
                    price = t.get('price', 0.0)
                    size = t.get('size', 0.0)
                    fees += price * size * 0.0005
    
    print(f"交易总数: {trade_count}")
    print(f"累计实现盈亏 (不含手续费估算): {realized_pnl}")
    print(f"估算手续费: {fees}")
    print(f"理论账户余额 (Balance): {initial_balance + realized_pnl - fees}")

    # 3. Load State
    if os.path.exists(state_file):
        with open(state_file, 'r', encoding='utf-8') as f:
            state = json.load(f)
            print(f"状态文件权益 (Equity): {state.get('equity')}")
            print(f"状态文件持仓 (ETH): {state.get('total_position_eth')}")
            print(f"状态文件多仓均价: {state.get('long_avg_price')}")
            print(f"状态文件空仓均价: {state.get('short_avg_price')}")

if __name__ == "__main__":
    check_consistency()
