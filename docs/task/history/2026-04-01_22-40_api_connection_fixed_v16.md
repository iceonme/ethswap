# 验收文档 - API 连接故障彻底解决方案 (v1.6)

**日期**: 2026-04-01 22:40
**版本**: v1.6 (Final)
**任务描述**: 解决“刚刚还好好的，突然连不上 API”的突发性联网障碍。

## 核心修复内容 (v1.6)

### 1. 移除 Eventlet 联网冲突 (Critical)
- **问题**: 在 Windows 下运行程序时，频繁出现 API 连接超时或 `WSAENETUNREACH`（网络不可达）报错。
- **原因**: 
    - 程序入口处使用了 `eventlet.monkey_patch()`。
    - **原理性冲突**: 在 Windows 环境中，`eventlet` 的协程补丁会接管 Python 的底层 `socket` 和 `ssl` 逻辑。这与 `requests` 库（以及可能存在的系统代理，如 Clash/TUN 模式）存在严重的兼容性问题，导致连接池无法正常初始化。
- **修复**: 
    - 已在 `run_eth_swap.py` 和 `dashboard/server.py` 中注释掉 `eventlet` 及其补丁逻辑。
    - **回归稳定模式**: 改为使用 Python 标准的 `threading` 模式。对于目前的单用户监控面板，标准模式比协程模式更稳定、兼容性更好。

### 2. 增强 API 错误反馈
- **优化**: 在 `infra/okx/client.py` 中增加了详细的请求日志。如果以后再发生网络问题，控制台会直接打印具体的报错细节（DNS 错误、超时还是 SSL 握手失败），不再静默失败。

## 验证结果

1. **连接测试**: 执行 `www.okx.com` 连通性测试，状态码 200，延迟约 1.5s，连接已恢复。
2. **Dashboard**: 面板启动后 WebSocket 同步正常，无 `WinError 233` 管道报错。
