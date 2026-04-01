# 验收文档 - 修复 Dashboard 交易记录实时同步问题

**日期**: 2026-04-01 21:55
**任务描述**: 修复成交记录在成功保存到 JSON 文件后，无法实时在 Dashboard UI 的“成交历史”表格中显示的问题。

## 修改内容

### [Engine]

#### [live.py](file:///c:/Projects/ethswap/engines/live.py)
在核心数据处理逻辑 `_on_data` 中，在组装状态对象时增加了从 `HistoryService` 获取增量载荷的操作。这确保了每当有新成交（成交后 `HistoryService` 会重置已发送标志）时，最新的交易列表都会被包含在下一秒的状态推送中。

```python
# live.py:206 追加逻辑
status = self.status_svc.build_status(...)

# [核心修复] 获取并合并历史载荷（含成交记录）
payload = self.history.get_history_payload(max_points=500)
status.update(payload)

self._notify_status(status)
```

## 验证结果

1. **持久化验证**: 检查 `v95_trades_paper.json`，确认成交记录（由 `OKXPaperExecutor` 产生）已实时写入。
2. **同步验证**: 启动系统后，模拟/实际成交发生时，观察 Dashboard。现在“成交历史”表格会随着控制台日志同步刷新。
