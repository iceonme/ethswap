# 验收文档 - 彻底修复 Dashboard 联动、收益率、标志与记录丢失问题

**日期**: 2026-04-01 22:00
**任务描述**: 修复前端 Dashboard 存在的四个核心 Bug：图表不联动、收益率不显示、K 线上没有交易标志、以及刷新后交易记录丢失。

## 修改内容

### 1. 图表联动修复 (Synchronization)
- **文件**: [charts.js](file:///c:/Projects/ethswap/dashboard/static/js/charts.js)
- **修复**: 正确映射了 `mainChart`, `rsiChart`, `equityChart` 的系列对象，解决了 `otherChart.series` 未定义导致的 `setCrosshairPosition` 调用失效。
- **效果**: 鼠标在主图上移动时，RSI 和资产曲线也会同步显示对应时间点的数值。

### 2. 收益率显示优化
- **文件**: [socket_handler.js](file:///c:/Projects/ethswap/dashboard/static/js/socket_handler.js)
- **修复**: 增加了 `initial_balance` 的全局缓存（`window.initialBalance`），即使后端推送包中偶尔缺失，前端也能基于缓存计算 PNL%。
- **效果**: 页面顶部的总资产下方现在能正确显示收益率（百分比）。

### 3. K 线交易标志竞态处理 (Race Condition)
- **文件**: [charts.js](file:///c:/Projects/ethswap/dashboard/static/js/charts.js)
- **修复**: 增加了 `window.pendingTrades` 缓冲区。如果交易数据在图表尚未完全就绪（`isChartReady` 为 false）时到达，将自动暂存并在就绪后立即补绘。
- **效果**: 解决了初次加载或刷新页面后，K 线图上经常缺失买卖箭头的 Bug。

### 4. 交易记录同步策略调整
- **文件**: [history.py](file:///c:/Projects/ethswap/services/history.py), [server.py](file:///c:/Projects/ethswap/dashboard/server.py)
- **修复**: 将交易记录和初始资金的推送逻辑与 K 线历史的 `_history_sent` 标志解关，确保关键状态在每次 Payload 请求中都能同步给 Dashboard，防止刷新页面后数据变为空。
- **效果**: 无论如何刷新页面或断开连接，交易记录和标志都能稳定持久地显示。

## 验证结果

1. **联动验证**: 实测鼠标十字光标在三个图表间同步移动，无报错。
2. **数据显示**: PNL% 补齐，显示为 `+X.XX%`。
3. **标志验证**: 确认 `[Charts] 生成了 X 个买卖标记` 日志输出，K 线图标记出现。
4. **刷新验证**: 刷新 Dashboard，数据瞬间同步，未出现丢失。
