import os

log_path = 'logs/v93_innovation_5100.log'
if os.path.exists(log_path):
    with open(log_path, 'rb') as f:
        # Read last 2 KB
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 4000))
        content = f.read()
        try:
            # Try to decode gbk or utf-8
            print(content.decode('gbk'))
        except:
            print(content.decode('utf-8', errors='ignore'))
else:
    print("Log not found")
