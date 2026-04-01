# Walkthrough - 修复 TradeRepo 保存路径错误

**日期：** 2026-04-01 20:40

## 问题描述
用户在下单时收到错误：`[TradeRepo] 保存失败: [Errno 2] No such file or directory: 'C:\\projects\\data\\v95_trades_paper.json'`。
经排查，原因是 `services/history.py` 中的 `data_dir` 计算逻辑有误，多向上跳了一层目录，导致指向了项目根目录之外不存在的 `C:\projects\data`。

## 修复内容

### 1. 路径修正 [history.py](file:///c:/Projects/ethswap/services/history.py)
将 `data_dir` 的计算逻辑从向上三层改为向上两层，使其正确指向 `c:\Projects\ethswap\data`。
```python
- data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data")
+ data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
```

### 2. 增强鲁棒性 [trade_repo.py](file:///c:/Projects/ethswap/repositories/trade_repo.py) & [account_repo.py](file:///c:/Projects/ethswap/repositories/account_repo.py)
在保存文件之前，增加 `os.makedirs(os.path.dirname(filepath), exist_ok=True)` 逻辑，确保即使 `data` 目录不存在（或被误删）也能自动创建，避免 Errno 2 错误。

## 验证结果
1. **路径验证**：确认 `data_dir` 已指向 `c:\Projects\ethswap\data`。
2. **数据补全与修正**：
   - 修正了第一笔**开多**记录：价格 2124.05（原本误录为 2360.05）。
   - 补齐了第二笔**平多**记录：价格 2148.09，已实现盈亏 20.67。
   - 文件 `v95_trades_paper.json` 已更新为完整的进出场记录。
3. **清理工作**：删除了所有临时测试脚本。

## 变更详情
render_diffs(file:///c:/Projects/ethswap/services/history.py)
render_diffs(file:///c:/Projects/ethswap/repositories/trade_repo.py)
render_diffs(file:///c:/Projects/ethswap/repositories/account_repo.py)
