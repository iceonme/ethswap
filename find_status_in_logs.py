import os

log_path = 'logs/v93_innovation_5100.log'
if os.path.exists(log_path):
    with open(log_path, 'rb') as f:
        content = f.read()
        # Try both common encodings
        try:
            text = content.decode('gbk')
        except:
            text = content.decode('utf-8', errors='ignore')
        
        lines = text.splitlines()
        for line in lines[-200:]: # Look at the last 200 lines
            if '已加载' in line or '重建' in line or 'Position:' in line or '持仓:' in line:
                print(line)
else:
    print("Log file not found")
