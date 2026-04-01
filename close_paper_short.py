import os
import sys
import json
import time
from datetime import datetime, timezone

# 确保项目目录在路径中
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

# 这个脚本专门用来为 5100 Paper 环境平掉空单
def main():
    print("="*50)
    print("    正在清理 5100 Paper 环境的空头被套仓位")
    print("="*50)
    
    trades_file = os.path.join(CURRENT_DIR, 'data', 'v93_trades_paper.json')
    if not os.path.exists(trades_file):
        print(f"[错误] 未找到 Paper 交易记录: {trades_file}")
        return
        
    try:
        with open(trades_file, 'r', encoding='utf-8') as f:
            trades = json.load(f)
    except Exception as e:
        print(f"[错误] 读取失败: {e}")
        return
        
    # 计算当前空头的净持仓（顺便计算多头可以用来对比）
    short_size = 0.0
    for t in sorted(trades, key=lambda x: x.get('t', 0)):
        meta = t.get('meta', {})
        action = t.get('action', '')
        size = float(t.get('size', 0))
        
        # 判断 posSide
        pos_side = 'long'
        if '短' in action or '空' in action or meta.get('posSide') == 'short':
            pos_side = 'short'
            
        is_open = '开' in action or 'entry' in t.get('reason', '').lower()
        
        if pos_side == 'short':
            if is_open:
                short_size += size
            else:
                short_size = max(0.0, short_size - size)
                
    if short_size <= 1e-8:
        print("[INFO] 当前没有空头仓位需要清理。")
        return
        
    print(f"\n[INFO] 检测到 Paper 环境当前有空头仓位大小: {short_size:.4f} ETH")
    print("[ACTION] 正在向本地交易记录中追加一条【强制平空】记录...")
    
    # 构造一条强制平空的模拟交易记录
    current_ms = int(time.time() * 1000)
    dt_now = datetime.now(timezone.utc)
    # 取最后一次交易的价格作为临时平仓价
    last_price = 1900.0
    if trades:
        last_price = trades[-1].get('price', 1900.0)
        
    fake_trade = {
        'type': 'BUY',       # 买入
        'action': '平空',    # 动作：平掉空头
        'symbol': 'ETH-USDT-SWAP',
        'price': last_price,
        'size': short_size,
        'quote_amount': last_price * short_size,
        'margin': 0.0,
        'pnl': 0.0,          # 强制平仓不计算损益
        'time': dt_now.isoformat(),
        't': current_ms,
        'detail': f"[脚本平仓] 平空 数量={short_size:.4f} 价格={last_price:.2f}",
        'reason': "由于系统转向单向排他模式，用户脚本强制平掉前期被困的空单",
        'meta': {
            'trade_id': f"script_close_{current_ms}",
            'ord_id': f"ord_{current_ms}",
            'source': 'paper',
            'posSide': 'short'
        }
    }
    
    trades.append(fake_trade)
    trades.sort(key=lambda x: x.get('t', 0))
    
    with open(trades_file, 'w', encoding='utf-8') as f:
        json.dump(trades, f, indent=4, ensure_ascii=False)
        
    print("\n[SUCCESS] 平仓记录追加成功！")
    print("您现在可以直接重新启动 run_ethswap_paper.py (5100端口)，")
    print("引擎加载时会自动读取到这条平仓记录，从而将空仓算作清零！")

if __name__ == "__main__":
    main()
