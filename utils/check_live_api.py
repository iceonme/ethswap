"""
实盘 API 连接测试脚本
"""
import sys
import os

# 确保项目目录在路径中
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from config.api_config import LIVE_CONFIG, DEFAULT_SYMBOL
from config.okx_config import OKXAPI

def check_live_api():
    print("\n" + "="*60)
    print("OKX 实盘 API 连接测试")
    print("="*60)
    
    if LIVE_CONFIG['api_key'] == 'YOUR_REAL_API_KEY':
        print("[错误] 请先在 config/api_config.py 中配置 LIVE_CONFIG 的 API 密钥！")
        return

    api = OKXAPI(
        api_key=LIVE_CONFIG['api_key'],
        api_secret=LIVE_CONFIG['api_secret'],
        passphrase=LIVE_CONFIG['passphrase'],
        is_demo=False
    )
    
    print("[1/3] 测试账户配置获取...")
    config = api.get_account_config()
    if config:
        print(f"  成功! 账户 UID: {config.get('uid')}")
        print(f"  权限: {config.get('perm')}")
    else:
        print("  失败! 无法获取账户配置，请检查 API 密钥。")
        return

    print("\n[2/3] 获取账户余额 (USDT)...")
    bal = api.get_balance('USDT')
    if bal:
        print(f"  可用余额: {bal['availBal']} USDT")
        print(f"  账户权益: {bal['eq']} USDT")
    else:
        print("  失败! 无法获取余额。")

    print("\n[3/3] 获取当前行情 (ETH-USDT-SWAP)...")
    ticker = api.get_ticker('ETH-USDT-SWAP')
    if ticker:
        print(f"  最新成交价: {ticker['last']} USDT")
    else:
        print("  失败! 无法获取行情。")

    print("\n" + "="*60)
    print("测试完成!")
    print("="*60)

if __name__ == "__main__":
    check_live_api()
