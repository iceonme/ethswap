import sys
import os
import pandas as pd
import numpy as np

# 路径设置
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from config.api_config import OKX_CONFIG
from config.okx_config import OKXAPI

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0)[-period:]
    losses = np.where(deltas < 0, -deltas, 0)[-period:]
    avg_gain = np.mean(gains) if len(gains) > 0 else 0
    avg_loss = np.mean(losses) if len(losses) > 0 else 0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def main():
    api = OKXAPI(
        api_key=OKX_CONFIG['api_key'],
        api_secret=OKX_CONFIG['api_secret'],
        passphrase=OKX_CONFIG['passphrase'],
        is_demo=True
    )
    
    # 1. 获取最近 K 线
    df = api.get_candles(limit=400)
    if df is None or df.empty:
        print("Error: Could not fetch candles")
        return
        
    current_price = df['close'].iloc[-1]
    last_ts = df.index[-1]
    last_rsi = calculate_rsi(df['close'].values)
    
    # 2. 计算网格 (复刻 V93 逻辑)
    lookback = 360
    recent = df.tail(lookback)
    highs = recent['high'].nlargest(5).values
    lows = recent['low'].nsmallest(5).values
    grid_top = np.mean(np.sort(highs)[1:])
    grid_bottom = np.mean(np.sort(lows)[:-1])
    
    step = (grid_top - grid_bottom) / 5
    entity_grids = [grid_bottom + i * step for i in range(6)]
    
    # 3. 确定层级
    layer = None
    for idx in range(5):
        if entity_grids[idx] <= current_price < entity_grids[idx+1]:
            layer = idx
            break
            
    # 4. 输出分析
    print("-" * 40)
    print(f"数据时间: {last_ts}")
    print(f"当前价格: {current_price:.2f}")
    print(f"当前 RSI: {last_rsi:.2f}")
    print(f"网格区间: [{grid_bottom:.2f}, {grid_top:.2f}]")
    print(f"当前层级: {layer if layer is not None else '超出网格'}")
    
    if layer is not None:
        layer_mid = (entity_grids[layer] + entity_grids[layer+1]) / 2
        print(f"当前层中线: {layer_mid:.2f}")
        
        # 简单判断
        if layer == 2:
            if current_price <= layer_mid:
                print("状态: 处于做多观察区 (需 RSI <= 35)")
                if last_rsi <= 35:
                    print("结论: 符合做多入场条件!")
                else:
                    print(f"结论: 价格达标，但 RSI ({last_rsi:.1f}) 过高")
            else:
                print("状态: 处于中轴上方，等待回调")
        elif layer <= 1:
            if current_price >= layer_mid:
                print("状态: 处于做空观察区 (需 RSI >= 65)")
                if last_rsi >= 65:
                    print("结论: 符合做空入场条件!")
                else:
                    print(f"结论: 价格达标，但 RSI ({last_rsi:.1f}) 过低")
    else:
        print("状态: 价格超出 5 层核心网格")

if __name__ == "__main__":
    main()
