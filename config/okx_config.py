"""
OKX交易所接入配置 (ETH Swap 专用版)
支持：模拟盘、实盘、合约特定操作
"""

import pandas as pd
import numpy as np
import time
import hmac
import hashlib
import base64
import json
import requests
from datetime import datetime, timezone


class OKXAPI:
    """OKX API接入类"""
    
    DEMO_API_URL = "https://www.okx.com"
    LIVE_API_URL = "https://www.okx.com"
    
    def __init__(self, api_key=None, api_secret=None, passphrase=None, 
                 is_demo=True, simulate_slippage=False):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.is_demo = is_demo
        self.simulate_slippage = simulate_slippage
        
        self.base_url = self.DEMO_API_URL if is_demo else self.LIVE_API_URL
        self.session = requests.Session()
        
        print(f"OKX API初始化完成 | 模式: {'模拟盘' if is_demo else '实盘'} | 子项目: ethswap")
        
    def _get_timestamp(self):
        return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    
    def _sign(self, timestamp, method, request_path, body=''):
        message = timestamp + method.upper() + request_path + body
        mac = hmac.new(
            self.api_secret.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256
        )
        d = mac.digest()
        return base64.b64encode(d).decode('utf-8')
    
    def _request(self, method, path, params=None, body=None):
        url = self.base_url + path
        request_path = path
        if method == 'GET' and params:
            from urllib.parse import urlencode
            query = urlencode(params)
            request_path += f"?{query}"
        
        body_str = ""
        if body:
            body_str = json.dumps(body)
            
        timestamp = self._get_timestamp()
        headers = {
            'OK-ACCESS-KEY': self.api_key,
            'OK-ACCESS-SIGN': self._sign(timestamp, method, request_path, body_str),
            'OK-ACCESS-TIMESTAMP': timestamp,
            'OK-ACCESS-PASSPHRASE': self.passphrase,
            'Content-Type': 'application/json'
        }
        if self.is_demo:
            headers['x-simulated-trading'] = '1'
        
        max_retries = 3
        retry_delay = 1
        
        for attempt in range(max_retries):
            try:
                if method == 'GET':
                    response = self.session.get(url, headers=headers, params=params, timeout=10)
                else:
                    response = self.session.post(url, headers=headers, data=body_str, timeout=10)
                
                # 处理 HTTP 错误 (5xx)
                if response.status_code >= 500:
                    print(f"[API 重试] HTTP {response.status_code} | 路径: {path} | 尝试 {attempt+1}/{max_retries}")
                    time.sleep(retry_delay)
                    retry_delay *= 2
                    continue

                res_json = response.json()
                
                # 处理业务错误 (50001)
                if res_json.get('code') != '0':
                    code = res_json.get('code')
                    msg = res_json.get('msg')
                    
                    # 50001: Service temporarily unavailable
                    if code == '50001' and attempt < max_retries - 1:
                        print(f"[API 重试] 业务错误 {code} ({msg}) | 路径: {path} | 尝试 {attempt+1}/{max_retries}")
                        time.sleep(retry_delay)
                        retry_delay *= 2
                        continue
                        
                    print(f"[OKX API ERROR] {path} | Code: {code} | Msg: {msg}")
                    if 'data' in res_json and len(res_json['data']) > 0:
                        d = res_json['data'][0]
                        if 'sMsg' in d or 'sCode' in d:
                            print(f"  [Server Detail] sCode: {d.get('sCode')} | sMsg: {d.get('sMsg')}")
                    print(f"  [Request Body] {body_str}")
                
                return res_json
                
            except Exception as e:
                if attempt < max_retries - 1:
                    print(f"[API 重试] 请求异常: {e} | 路径: {path} | 尝试 {attempt+1}/{max_retries}")
                    time.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    print(f"OKX API 请求最终失败 [{path}]: {e}")
                    return None
        return None
    
    def get_balance(self, ccy='USDT'):
        result = self._request('GET', '/api/v5/account/balance', {'ccy': ccy})
        if result and result.get('code') == '0':
            data = result['data'][0]
            details = data.get('details', [])
            for asset in details:
                if asset['ccy'] == ccy:
                    return {
                        'availBal': float(asset['availBal']),
                        'eq': float(asset['eq']),
                        'raw': data
                    }
        return None

    def get_account_config(self):
        """获取账户配置信息 (含 UID)"""
        result = self._request('GET', '/api/v5/account/config')
        if result and result.get('code') == '0':
            return result.get('data', [{}])[0]
        return None

    def get_balances(self):
        result = self._request('GET', '/api/v5/account/balance')
        if result and result.get('code') == '0':
            data = result['data'][0]
            return {
                'details': data.get('details', []),
                'totalEq': float(data.get('totalEq', 0) or 0),
                'raw': data
            }
        return None
    
    def get_ticker(self, inst_id='ETH-USDT-SWAP'):
        result = self._request('GET', '/api/v5/market/ticker', {'instId': inst_id})
        if result and result.get('code') == '0':
            return result['data'][0]
        return None
    
    def get_candles(self, inst_id='ETH-USDT-SWAP', bar='1m', limit=100):
        """获取K线数据, 支持超过100个的分页查询"""
        all_data = []
        last_ts = ""
        
        remaining = limit
        while remaining > 0:
            current_limit = min(remaining, 100)
            params = {'instId': inst_id, 'bar': bar, 'limit': str(current_limit)}
            
            if not last_ts:
                path = '/api/v5/market/candles'
            else:
                path = '/api/v5/market/history-candles'
                params['after'] = last_ts
                
            result = self._request('GET', path, params)
            if result and result.get('code') == '0' and result.get('data'):
                batch = result['data']
                all_data.extend(batch)
                if len(batch) < current_limit:
                    break
                last_ts = batch[-1][0]
                remaining -= len(batch)
            else:
                break
                
        if all_data:
            df = pd.DataFrame(all_data, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 'volume', 'volCcy', 'volCcyQuote', 'confirm'
            ])
            df['timestamp'] = pd.to_datetime(df['timestamp'].astype(float), unit='ms', utc=True)
            df.set_index('timestamp', inplace=True)
            df = df.sort_index()
            df = df[['open', 'high', 'low', 'close', 'volume']].astype(float)
            return df
        return None
    
    def set_leverage(self, inst_id, lever, mgn_mode='cross'):
        """设置杠杆倍数"""
        body = {
            'instId': inst_id,
            'lever': str(lever),
            'mgnMode': mgn_mode
        }
        return self._request('POST', '/api/v5/account/set-leverage', body=body)

    def set_position_mode(self, pos_mode='net_mode'):
        """设置持仓模式：long_short_mode (双向) / net_mode (单向)"""
        body = {'posMode': pos_mode}
        return self._request('POST', '/api/v5/account/set-position-mode', body=body)

    def place_order(self, inst_id, side, ord_type, sz, px=None, 
                    td_mode='cross', pos_side=None, ccy=None, cl_ord_id=None, force_server=True):
        body = {
            'instId': inst_id,
            'tdMode': td_mode,
            'side': side,
            'ordType': ord_type,
            'sz': str(sz)
        }
        if pos_side:
            body['posSide'] = pos_side
        if px and ord_type == 'limit':
            body['px'] = str(px)
        if ccy:
            body['ccy'] = ccy
        if cl_ord_id:
            body['clOrdId'] = cl_ord_id
        return self._request('POST', '/api/v5/trade/order', body=body)
    
    def close_position(self, inst_id, mgn_mode='cross', pos_side=None):
        """市价平掉指定方向的仓位"""
        body = {
            'instId': inst_id,
            'mgnMode': mgn_mode
        }
        if pos_side:
            body['posSide'] = pos_side
        return self._request('POST', '/api/v5/trade/close-position', body=body)

    def get_positions(self, inst_id=None, inst_type='SWAP'):
        params = {'instType': inst_type}
        if inst_id:
            params['instId'] = inst_id
        result = self._request('GET', '/api/v5/account/positions', params)
        if result and result.get('code') == '0':
            return result['data']
        return []

    def get_order_history(self, inst_id, limit=100, inst_type='SWAP'):
        params = {'instType': inst_type, 'instId': inst_id, 'limit': str(limit)}
        return self._request('GET', '/api/v5/trade/orders-history', params)

    def get_fills(self, inst_id, limit=100, inst_type='SWAP'):
        """获取最近成交明细"""
        params = {'instType': inst_type, 'instId': inst_id, 'limit': str(limit)}
        return self._request('GET', '/api/v5/trade/fills-history', params)

    def get_bills(self, inst_type='SWAP', ccy=None, limit=100):
        """获取账单流水 (用于重构净值曲线)"""
        params = {'instType': inst_type, 'limit': str(limit)}
        if ccy:
            params['ccy'] = ccy
        return self._request('GET', '/api/v5/account/bills', params)
