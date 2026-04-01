import os
import sys
import json
import time
from datetime import datetime, timezone

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

# 确保完全清理所有持仓
def main():
    print("="*50)
    print("    正在全面清理 5100 Paper 环境的所有持仓")
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
        
    # 2026-03-31 增强：在清理前执行一次智能去重
    seen_ord_ids = set()
    cleaned_trades = []
    dedup_count = 0
    
    for t in sorted(trades, key=lambda x: x.get('t', 0)):
        oid = t.get('meta', {}).get('ord_id') or t.get('meta', {}).get('trade_id')
        if oid:
            if oid in seen_ord_ids:
                dedup_count += 1
                continue
            seen_ord_ids.add(oid)
        cleaned_trades.append(t)
    
    if dedup_count > 0:
        print(f"[INFO] 监测到并已剔除 {dedup_count} 条重复成交记录")
        trades = cleaned_trades

    long_size = 0.0
    short_size = 0.0
    
    # 我们用非常严紧的 OKX 语义重构一遍仓位
    for t in trades:
        meta = t.get('meta', {})
        action = t.get('action', '')
        size = float(t.get('size', 0))
        
        pos_side = 'long'
        if '短' in action or '空' in action or meta.get('posSide') == 'short':
            pos_side = 'short'
            
        is_open = '开' in action or 'entry' in t.get('reason', '').lower()
            
        if pos_side == 'long':
            if is_open: long_size += size
            else: long_size = max(0.0, long_size - size)
        else:
            if is_open: short_size += size
            else: short_size = max(0.0, short_size - size)
                
    print(f"\n[INFO] 去重后推导出的当前底仓:")
    print(f"  - 多头: {long_size:.4f} ETH")
    print(f"  - 空头: {short_size:.4f} ETH")
    
    if long_size <= 1e-8 and short_size <= 1e-8:
        if dedup_count > 0:
             print("[INFO] 去重后发现仓位已清空，正在保存清理后的历史...")
             with open(trades_file, 'w', encoding='utf-8') as f:
                 json.dump(trades, f, indent=4, ensure_ascii=False)
             print("[SUCCESS] 历史数据已去重，引擎现在是干净的。")
        else:
             print("[INFO] 当前没有任何仓位（且无重复记录），引擎已经是干净的。")
        return

    current_ms = int(time.time() * 1000)
    dt_now = datetime.now(timezone.utc)
    last_price = trades[-1].get('price', 1900.0) if trades else 1900.0
    
    # 强制加两条平仓指令（不管三七二十一平掉两者）
    if long_size > 1e-8:
        trades.append({
            'type': 'SELL',
            'action': '平多',
            'symbol': 'ETH-USDT-SWAP',
            'price': last_price,
            'size': long_size,
            'quote_amount': last_price * long_size,
            'margin': 0.0,
            'pnl': 0.0,
            'time': dt_now.isoformat(),
            't': current_ms,
            'detail': f"[脚本平仓] 半路杀出程咬金 平多 {long_size:.4f}",
            'reason': '强制清理旧的多单',
            'meta': {'trade_id': f"clear_long_{current_ms}", 'ord_id': f"cl_{current_ms}", 'source': 'paper', 'posSide': 'long'}
        })
        print(f"[ACTION] 注入平多记录 {long_size:.4f}")

    if short_size > 1e-8:
        trades.append({
            'type': 'BUY',
            'action': '平空',
            'symbol': 'ETH-USDT-SWAP',
            'price': last_price,
            'size': short_size,
            'quote_amount': last_price * short_size,
            'margin': 0.0,
            'pnl': 0.0,
            'time': dt_now.isoformat(),
            't': current_ms + 1,
            'detail': f"[脚本平仓] 半路杀出程咬金 平空 {short_size:.4f}",
            'reason': '强制清理旧的空单',
            'meta': {'trade_id': f"clear_short_{current_ms}", 'ord_id': f"cs_{current_ms}", 'source': 'paper', 'posSide': 'short'}
        })
        print(f"[ACTION] 注入平空记录 {short_size:.4f}")

    trades.sort(key=lambda x: x.get('t', 0))
    with open(trades_file, 'w', encoding='utf-8') as f:
        json.dump(trades, f, indent=4, ensure_ascii=False)
        
    print("\n[SUCCESS] 所有仓位已强行平掉！请立刻重启 run_ethswap_paper.py！")

if __name__ == "__main__":
    main()
