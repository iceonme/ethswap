# 验收文档 - 修复 OKXPaperExecutor 启动重建状态时的 NoneType 报错

**日期**: 2026-04-01 21:05
**任务描述**: 修复在模拟执行器启动并从历史记录重建状态时，因 `pnl` 字段为 `null` (None) 导致的 `float()` 转换异常。

## 修改内容

### [Executor]

#### [okx_paper.py](file:///c:/Projects/ethswap/executors/okx_paper.py)
对 `reconstruct_state` 方法中的 `price`, `size`, `pnl` 取值逻辑进行了增强，使用 `value or 0.0` 确保在字段值为 `None` 时能回退到浮点数 `0.0`。

```python
# 修复前
price = float(trade.get('price', 0))
size = float(trade.get('size', 0))
pnl = float(trade.get('pnl', 0))

# 修复后 (防御性写法)
price = float(trade.get('price') or 0.0)
size = float(trade.get('size') or 0.0)
pnl = float(trade.get('pnl') or 0.0)
```

## 验证结果

使用模拟历史数据（包含 `pnl: null`）运行了验证脚本 `verify_fix.py`，结果如下：

```text
Testing reconstruct_state...
[Paper] 正在从 2 条历史记录重建持仓状态...
[Paper] 重建完成。有效持仓方向: 0, 可用资金: 10011.11
Success! No TypeError.
Final cash: 10011.111775
```

系统现在可以完美跨越历史重建阶段并正常启动。
