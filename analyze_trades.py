import json
from datetime import datetime

class Position:
    def __init__(self, size=0, avg_price=0):
        self.size = size
        self.avg_price = avg_price

def mimic_reconstruct(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        trades = json.load(f)
    
    positions = {} # (symbol, pos_side) -> Position
    
    # Sort by time
    sorted_trades = sorted(trades, key=lambda x: x.get('t', 0))
    
    for trade in sorted_trades:
        symbol = trade.get('symbol')
        action = trade.get('action', '')
        size = float(trade.get('size', 0))
        price = float(trade.get('price', 0))
        pnl = float(trade.get('pnl', 0))
        
        pos_side = 'long'
        if '空' in action or trade.get('meta', {}).get('posSide') == 'short':
            pos_side = 'short'
            
        is_open = '开' in action
        
        pos_key = (symbol, pos_side)
        if pos_key not in positions:
            positions[pos_key] = Position()
            
        pos = positions[pos_key]
        
        if is_open:
            new_size_abs = abs(pos.size) + size
            if new_size_abs > 0:
                pos.avg_price = (abs(pos.size) * pos.avg_price + size * price) / new_size_abs
            else:
                pos.avg_price = 0
            pos.size = new_size_abs if pos_side == 'long' else -new_size_abs
        else:
            new_size_abs = max(0, abs(pos.size) - size)
            if new_size_abs < 1e-8:
                pos.avg_price = 0
                pos.size = 0
            else:
                # pos.avg_price doesn't change on close
                pos.size = new_size_abs if pos_side == 'long' else -new_size_abs
                
    print("Reconstructed Positions (Mimicking MockSwapExecutor):")
    for key, pos in positions.items():
        if abs(pos.size) > 1e-8:
            print(f"  {key}: size={pos.size:.4f}, avg_price={pos.avg_price:.2f}")

if __name__ == "__main__":
    mimic_reconstruct('data/v93_trades_paper.json')
