"""
OKX API 统一客户端 (Infrastructure Layer)
职责：底层的签名、请求、重试及 OKX 业务接口封装。
"""

import hmac
import hashlib
import base64
import json
import time
import requests
import pandas as pd
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

class OKXClient:
    """OKX API 接入客户端"""
    
    BASE_URL = "https://www.okx.com"
    
    def __init__(self, api_key: str, api_secret: str, passphrase: str, 
                 is_demo: bool = True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.is_demo = is_demo
        
        self.session = requests.Session()
        
    def _get_timestamp(self) -> str:
        return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    
    def _sign(self, timestamp: str, method: str, request_path: str, body: str = '') -> str:
        message = timestamp + method.upper() + request_path + body
        mac = hmac.new(
            self.api_secret.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256
        )
        return base64.b64encode(mac.digest()).decode('utf-8')
    
    def _request(self, method: str, path: str, params: Optional[Dict] = None, body: Optional[Dict] = None) -> Dict:
        url = self.BASE_URL + path
        request_path = path
        if method == 'GET' and params:
            from urllib.parse import urlencode
            query = urlencode(params)
            request_path += f"?{query}"
        
        body_str = json.dumps(body) if body else ""
            
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
                
                # 处理 HTTP 5xx 错误
                if response.status_code >= 500:
                    time.sleep(retry_delay)
                    retry_delay *= 2
                    continue

                res_json = response.json()
                
                # 处理业务重试 (50001: Service temporarily unavailable)
                if res_json.get('code') == '50001' and attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    retry_delay *= 2
                    continue
                
                return res_json
                
            except Exception as e:
                if attempt < max_retries - 1:
                    print(f"[OKX API] 请求异常 (重试 {attempt+1}/{max_retries}): {e}")
                    time.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    print(f"[OKX API] 请求彻底失败: {e}")
                    return {'code': '-1', 'msg': f'API request failed: {str(e)}'}
        return {'code': '-1', 'msg': 'Max retries exceeded'}

    # --- 账户与配置 ---
    
    def get_account_config(self) -> Optional[Dict]:
        res = self._request('GET', '/api/v5/account/config')
        return res.get('data', [{}])[0] if res and res.get('code') == '0' else None

    def get_balance(self, ccy: str = 'USDT') -> Optional[Dict]:
        res = self._request('GET', '/api/v5/account/balance', {'ccy': ccy})
        if res and res.get('code') == '0' and res.get('data'):
            data = res['data'][0]
            for asset in data.get('details', []):
                if asset['ccy'] == ccy:
                    return {'availBal': float(asset['availBal']), 'eq': float(asset['eq']), 'raw': data}
        return None

    def set_leverage(self, inst_id: str, lever: float, mgn_mode: str = 'cross') -> Dict:
        body = {'instId': inst_id, 'lever': str(lever), 'mgnMode': mgn_mode}
        return self._request('POST', '/api/v5/account/set-leverage', body=body)

    def set_position_mode(self, pos_mode: str = 'net_mode') -> Dict:
        return self._request('POST', '/api/v5/account/set-position-mode', body={'posMode': pos_mode})

    # --- 市场数据 ---
    
    def get_ticker(self, inst_id: str) -> Optional[Dict]:
        res = self._request('GET', '/api/v5/market/ticker', {'instId': inst_id})
        return res['data'][0] if res and res.get('code') == '0' else None

    def get_candles(self, inst_id: str, bar: str = '1m', limit: int = 100) -> pd.DataFrame:
        all_data = []
        last_ts = ""
        remaining = limit
        while remaining > 0:
            current_limit = min(remaining, 100)
            params = {'instId': inst_id, 'bar': bar, 'limit': str(current_limit)}
            path = '/api/v5/market/candles' if not last_ts else '/api/v5/market/history-candles'
            if last_ts: params['after'] = last_ts
                
            res = self._request('GET', path, params)
            if res and res.get('code') == '0' and res.get('data'):
                batch = res['data']
                all_data.extend(batch)
                if len(batch) < current_limit: break
                last_ts = batch[-1][0]
                remaining -= len(batch)
            else: break
                
        if all_data:
            df = pd.DataFrame(all_data, columns=['ts', 'open', 'high', 'low', 'close', 'vol', 'volCcy', 'volCcyQuote', 'confirm'])
            df['ts'] = pd.to_datetime(df['ts'].astype(float), unit='ms', utc=True)
            df.set_index('ts', inplace=True)
            df = df.sort_index()
            return df[['open', 'high', 'low', 'close', 'vol']].astype(float)
        return pd.DataFrame()

    # --- 交易业务 ---
    
    def place_order(self, inst_id: str, side: str, ord_type: str, sz: float, px: Optional[float] = None, 
                    td_mode: str = 'cross', pos_side: Optional[str] = None, cl_ord_id: Optional[str] = None) -> Dict:
        body = {'instId': inst_id, 'tdMode': td_mode, 'side': side, 'ordType': ord_type, 'sz': str(sz)}
        if pos_side: body['posSide'] = pos_side
        if px and ord_type == 'limit': body['px'] = str(px)
        if cl_ord_id: body['clOrdId'] = cl_ord_id
        return self._request('POST', '/api/v5/trade/order', body=body)
    
    def close_position(self, inst_id: str, mgn_mode: str = 'cross', pos_side: Optional[str] = None) -> Dict:
        body = {'instId': inst_id, 'mgnMode': mgn_mode, 'autoCxl': True}
        if pos_side: body['posSide'] = pos_side
        return self._request('POST', '/api/v5/trade/close-position', body=body)

    def get_positions(self, inst_id: Optional[str] = None, inst_type: str = 'SWAP') -> List[Dict]:
        params = {'instType': inst_type}
        if inst_id: params['instId'] = inst_id
        res = self._request('GET', '/api/v5/account/positions', params)
        return res.get('data', []) if res and res.get('code') == '0' else []

    def get_order_history(self, inst_id: str, limit: int = 100, inst_type: str = 'SWAP') -> Dict:
        params = {'instType': inst_type, 'instId': inst_id, 'limit': str(limit)}
        return self._request('GET', '/api/v5/trade/orders-history', params)

    def get_fills(self, inst_id: str, limit: int = 100, inst_type: str = 'SWAP') -> Dict:
        params = {'instType': inst_type, 'instId': inst_id, 'limit': str(limit)}
        return self._request('GET', '/api/v5/trade/fills-history', params)

    def get_bills(self, inst_type: str = 'SWAP', ccy: Optional[str] = None, limit: int = 100) -> Dict:
        params = {'instType': inst_type, 'limit': str(limit)}
        if ccy: params['ccy'] = ccy
        return self._request('GET', '/api/v5/account/bills', params)
