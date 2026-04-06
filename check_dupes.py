import json
import os

def check_duplicates():
    data_dir = 'data'
    trades_file = os.path.join(data_dir, 'v95_trades_paper.json')

    if not os.path.exists(trades_file):
        return

    with open(trades_file, 'r', encoding='utf-8') as f:
        trades = json.load(f)

    ids = {}
    for t in trades:
        ord_id = t.get('meta', {}).get('ord_id')
        if not ord_id:
            ord_id = f"no_id_{t.get('t')}"
        ids[ord_id] = ids.get(ord_id, 0) + 1

    duplicates = {k: v for k, v in ids.items() if v > 1}
    if duplicates:
        print(f"Found {len(duplicates)} duplicate order IDs:")
        for k, v in duplicates.items():
            print(f"  ID: {k}, Count: {v}")
    else:
        print("No duplicate order IDs found.")

if __name__ == "__main__":
    check_duplicates()
