import json
import os

def check_keys():
    config_path = 'config.v93final.json'
    if not os.path.exists(config_path):
        print(f"Error: {config_path} not found")
        return
        
    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)
        
    okx = config['okx']
    print(f"Keys in config:")
    for k, v in okx.items():
        if k in ['api_key', 'api_secret', 'passphrase']:
            print(f"{k}: {v}")
            print(f"  ASCII: {[ord(c) for c in v]}")

if __name__ == "__main__":
    check_keys()
