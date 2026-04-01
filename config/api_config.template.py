"""
ETH Swap API 配置文件模板
使用方法：
1. 复制本文件重命名为 `api_config.py`
2. 填入您的真实或模拟盘 API Key 信息
"""

# OKX API 配置 (模拟盘/实盘)
OKX_CONFIG = {
    'api_key': 'YOUR_API_KEY_HERE',
    'api_secret': 'YOUR_API_SECRET_HERE',
    'passphrase': 'YOUR_PASSPHRASE_HERE',
    'is_demo': True  # 设置为 True 为模拟盘，False 为实盘
}

# 实盘/其他用途配置 (可选)
LIVE_CONFIG = {
    'api_key': 'YOUR_LIVE_KEY_HERE',
    'api_secret': 'YOUR_LIVE_SECRET_HERE',
    'passphrase': 'YOUR_LIVE_PASSPHRASE_HERE',
    'is_demo': False
}

# 默认交易配置
DEFAULT_SYMBOL = 'ETH-USDT-SWAP'
DEFAULT_TIMEFRAME = '1m'
