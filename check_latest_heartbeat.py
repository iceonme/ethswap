import os

log_path = 'logs/v93_innovation_5100.log'
if os.path.exists(log_path):
    with open(log_path, 'rb') as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        # Read last 10 KB
        f.seek(max(0, size - 10000))
        content = f.read()
        try:
            text = content.decode('gbk')
        except:
            text = content.decode('utf-8', errors='ignore')
        
        lines = text.splitlines()
        heartbeats = [line for line in lines if '[心跳]' in line]
        if heartbeats:
            print(f"Latest Heartbeat: {heartbeats[-1]}")
        else:
            print("No heartbeat found in last 10KB")
else:
    print("Log not found")
