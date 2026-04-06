import json
import os

def analyze_v95():
    data_dir = 'data'
    trades_file = os.path.join(data_dir, 'v95_trades_paper.json')
    state_file = os.path.join(data_dir, 'v95_state.json')
    initial_balance_file = os.path.join(data_dir, 'v95_initial_balance_paper.json')

    if not os.path.exists(trades_file):
        print("No trades file.")
        return

    with open(trades_file, 'r', encoding='utf-8') as f:
        trades = json.load(f)

    initial_balance = 10000.0
    if os.path.exists(initial_balance_file):
        with open(initial_balance_file, 'r', encoding='utf-8') as f:
            initial_balance = json.load(f).get('initial_balance', 10000.0)

    print(f"All-time Initial Balance: {initial_balance}")

    sum_pnl_field = 0.0
    entry_fees = 0.0
    exit_fees = 0.0
    
    # We need to know if the 'pnl' field in trades already includes fees.
    # Looking at OKXPaperExecutor code:
    # Exit trade: pnl = (price - avg_price) * size * dir - fee
    # Entry trade: pnl = 0.0, but cash -= fee
    
    for t in trades:
        action = t.get('action', '')
        pnl = t.get('pnl', 0.0) or 0.0
        price = t.get('price', 0.0)
        size = t.get('size', 0.0)
        fee = price * size * 0.0005
        
        sum_pnl_field += pnl
        
        if '开' in action:
            entry_fees += fee
        elif '平' in action:
            exit_fees += fee

    print(f"Sum of 'pnl' fields in trades: {sum_pnl_field}")
    print(f"Calculated Sum of Entry Fees: {entry_fees}")
    print(f"Calculated Expected Balance: {initial_balance + sum_pnl_field - entry_fees}")

    if os.path.exists(state_file):
        with open(state_file, 'r', encoding='utf-8') as f:
            state = json.load(f)
            print(f"Current State Cash: {state.get('cash')}")
            print(f"Current State Equity: {state.get('equity')}")
            print(f"Current State Realized PnL: {state.get('realized_pnl')}")

if __name__ == "__main__":
    analyze_v95()
