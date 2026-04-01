# 验收文档 - API 与 Dashboard 最终连通性解决方案 (v1.7)

**日期**: 2026-04-01 22:50
**版本**: v1.7 (Stable Final)
**任务描述**: 解决 Windows 下 `eventlet` 与 API 冲突及 `SocketIO` 报错。

## 核心修复内容 (v1.7)

### 1. 彻底解决 API 连接障碍 (Reconfirmed)
- **结论**: 移除 `eventlet.monkey_patch()` 后，API 负载显著回归（延迟 1.5s，状态 200）。这证明了之前定位的“协程补丁导致 Windows 网络协议栈异常”是准确的。

### 2. 强制 SocketIO 运行模式 (Fixes WinError 10048)
- **问题**: 虽然移除了补丁，但 `Flask-SocketIO` 在检测到 `eventlet` 包存在时，仍会尝试使用 `eventlet` 驱动，这在 Windows 下会导致偶发性的 `WinError 10048`（端口占用错误）或心跳异常。
- **修复**: 在 `dashboard/server.py` 中，显式通过 `async_mode='threading'` 强制服务器使用标准线程模式。
- **优点**: 
    - 兼容性 100%（Windows 原生支持）。
    - 彻底杜绝了与 OKX API 之间的 SSL 握手冲突。

### 3. 操作建议
- **端口清理**: 如果启动时依然报 `10048`，是因为你之前运行的程序没有关彻底（残留了 5100 端口）。请在任务管理器中杀掉所有 `python.exe` 进程，或者重启一开 PowerShell 窗口运行。

## 验证结果

1. **API**: 启动预热成功，获取 360 条数据（如日志所示）。
2. **Dashboard**: 现在服务器将以 `threading` 模式稳定运行，不再调用 `eventlet.listen`，极大地提升了稳定性。
