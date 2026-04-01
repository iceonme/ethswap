# ETH Swap Trading Engine (v9.3)

这是一个基于 OKX API 的以太坊（ETH）永续合约自动交易引擎，集成了网格交易策略、实时风控监控以及可视化仪表盘。

## 主要特性

- **策略引擎**：支持多层网格拦截逻辑，防止重复开仓。
- **混合执行器**：支持模拟盘（Paper Trading）与实盘（Live Trading）无缝切换。
- **实时监控**：基于 Flask 和 Lightweight Charts 的 Web 仪表盘。
- **鲁棒性**：完善的日志记录、心跳检查以及异常状态恢复机制。
- **数据跟踪**：自动记录交易历史、净值曲线及 RSI 等技术指标。

## 项目结构

- `core/`: 核心逻辑组件。
- `engines/`: 交易引擎实现（含 Live/Mock 模式）。
- `strategies/`: 交易策略实现（如 V9.3 创新版网格）。
- `executors/`: 交易所接口封装。
- `dashboard/`: 可视化监控前端与后端。
- `utils/`: 通用工具类、API 诊断工具。
- `config/`: 配置文件（包含 API 密钥模板）。

## 快速开始

1. **环境准备**：
   ```bash
   pip install -r requirements.txt
   ```
2. **配置 API**：
   将 `config/api_config.template.py` 复制并重命名为 `config/api_config.py`，填写您的 OKX API 密钥。
3. **启动交易**：
   ```bash
   python run_ethswap_paper.py
   ```
4. **查看监控**：
   运行 `python dashboard/server.py`，访问 `http://localhost:5000`。

## 注意事项

- 本项目由 AI 辅助开发与优化。
- 交易有风险，请务必先在模拟盘进行充分测试。
- 请勿将包含真实 API 密钥的 `api_config.py` 上传至任何公开代码库。
