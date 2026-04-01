import json
import os
from datetime import datetime, timezone

class Position:
    def __init__(self, symbol, size, avg_price, entry_time):
        self.symbol = symbol
        self.size = size
        self.avg_price = avg_price
        self.entry_time = entry_time

def reconstruct_state(trade_records):
    positions = {}
    for trade in trade_records:
        symbol = trade.get('symbol')
        side = trade.get('type', '').lower()
        action = trade.get('action', '')
        price = float(trade.get('price', 0))
        size = float(trade.get('size', 0))
        pnl = float(trade.get('pnl', 0))
        
        pos_side = 'long'
        if '短' in action or '空' in action or trade.get('meta', {}).get('posSide') == 'short':
            pos_side = 'short'
        
        is_open = '开' in action or 'entry' in trade.get('reason', '').lower()
        
        pos_key = (symbol, pos_side)
        pos = positions.get(pos_key, Position(symbol=symbol, size=0, avg_price=0, entry_time=datetime.now(timezone.utc)))
        
        if is_open:
            new_size_abs = abs(pos.size) + size
            if new_size_abs > 0:
                new_avg_px = (abs(pos.size) * pos.avg_price + size * price) / new_size_abs
            else:
                new_avg_px = 0
            final_size = new_size_abs if pos_side == 'long' else -new_size_abs
        else:
            new_size_abs = max(0, abs(pos.size) - size)
            if new_size_abs < 1e-8:
                new_avg_px = 0
                final_size = 0
            else:
                new_avg_px = pos.avg_price
                final_size = new_size_abs if pos_side == 'long' else -new_size_abs
                
        positions[pos_key] = Position(symbol=symbol, size=final_size, avg_price=new_avg_px, entry_time=datetime.now(timezone.utc))
        print(f"Trade: {action} {size} @ {price} | Result: {pos_side} size={final_size}")

    return positions

with open('data/v93_trades_paper.json', 'r', encoding='utf-8') as f:
    trades = json.load(f)

final_positions = reconstruct_state(trades)
print("\nFinal State:")
for k, v in final_positions.items():
    if abs(v.size) > 1e-8:
        print(f"  {k}: {v.size}")
    else:
        print(f"  {k}: 0.0")
