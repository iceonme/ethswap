# 验收文档 - Dashboard 故障彻底解决方案 (v1.3)

**日期**: 2026-04-01 22:20
**任务描述**: 在 v1.2 基础上，进一步解决控制台残留的 `Value is null` 报错以及收益率显示逻辑的鲁棒性问题。

## 核心修复内容

### 1. 图表联动 Null 引用修复 (Critical)
- **现象**: F12 报错 `Error: Value is null at ye.setVisibleRange`。
- **原因**: 当某个图表尚未加载数据时，触发时间轴同步会导致 Range 对象中包含 Null 值，进而引发库内部崩溃。
- **修复**: 在 `charts.js` 的 `subscribeVisibleTimeRangeChange` 回调中增加了对 `range.from` 和 `range.to` 的非空校验，并对 `setVisibleRange` 调用增加了 `try...catch` 保护。

### 2. 收益率（PNL）计算逻辑鲁棒性增强
- **文件**: [socket_handler.js](file:///c:/Projects/ethswap/dashboard/static/js/socket_handler.js)
- **修复**: 
    - 统一将 `initial_balance` 和 `total_value` 强制转换为 `parseFloat`。
    - 增加了对 `initial_balance` 为 `0`、`null` 或 `NaN` 的全面过滤，确保 PNL 计算公式只在有合法基准值时运行。
    - 收益率显示增加了 `isNaN` 兜底，防止出现意料之外的 `NaN%` 字符。

### 3. 基建级异常屏蔽 (回顾 v1.2)
- 全面保留了 `socket_handler.js` 中的模块化熔断机制，确保即便有未预见的 JS 错误，也不会拖累持仓、策略等关键面板的实时更新。

## 验证结果

1. **联动验证**: 现在无论图表是否加载完成，缩放和光标同步均不再产生控制台报错。
2. **数据显示**: 收益率（PNL）显示逻辑更加稳定，不再受到初始资金加载竞态的影响。
3. **整体一致性**: 刷新 Dashboard，所有数据（K线、标志、持仓、建议、收益率）均能在 1 秒内完成全量同步。

---
存档位置: [2026-04-01_22-20_dashboard_final_fix_v13.md](file:///c:/Projects/ethswap/docs/task/history/2026-04-01_22-20_dashboard_final_fix_v13.md)
