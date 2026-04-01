/**
 * Dashboard UI 渲染与格式化模块
 */

// 全局状态
window.latestStrategyInfo = null;
window.tradePaginationState = {
    allTrades: [],
    currentPage: 1,
    pageSize: 20,
    totalPages: 1
};

// 资产显示逻辑
function renderAssetSummary(data) {
    const elInit = document.getElementById('initialBalance');
    if (elInit && data.initial_balance) {
        elInit.textContent = formatNumber(data.initial_balance) + ' USDT';
    }
}

// 格式化工具
function fmtPct(v) {
    if (v === undefined || v === null || Number.isNaN(Number(v))) return '--';
    return `${(Number(v) * 100).toFixed(2)}%`;
}

function fmtVal(v) {
    if (v === undefined || v === null) return '--';
    if (typeof v === 'boolean') return v ? '开启' : '关闭';
    if (typeof v === 'number') return Number.isInteger(v) ? `${v}` : `${v.toFixed(4)}`;
    return `${v}`;
}

function formatNumber(num, decimals = 2) {
    if (num === undefined || num === null || num === '--') return '--';
    const n = typeof num === 'string' ? parseFloat(num) : num;
    if (isNaN(n)) return '--';
    return n.toLocaleString('zh-CN', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

function formatLocalTime(timeStr) {
    if (!timeStr) return '--:--:--';
    try {
        const date = new Date(timeStr);
        return date.toLocaleString('zh-CN', {
            timeZone: 'Asia/Shanghai',
            year: 'numeric',
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
            hour12: false
        });
    } catch (e) {
        return '--:--:--';
    }
}

// 策略说明渲染
function renderStrategyDoc(strategyData) {
    const box = document.getElementById('strategyDocContent');
    if (!box) return;
    const params = (strategyData && strategyData.params) ? strategyData.params : null;
    if (!params) {
        box.innerHTML = `<div class="strategy-block"><p>等待策略参数同步（通常 2-3 秒内）。</p></div>`;
        return;
    }

    const blocks = [];
    blocks.push(`
        <div class="strategy-block">
            <h4>策略机制</h4>
            <p>这是一个动态网格 + RSI 过滤策略：先基于最近波动生成网格区间，再根据 RSI 与市场状态决定是否买卖、买卖多少。</p>
        </div>
    `);

    blocks.push(`
        <div class="strategy-block">
            <h4>当前参数 (V95 ETH Swap)</h4>
            <div class="param-grid" style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 8px;">
                <div class="param-item"><div class="param-name">交易对</div><div class="param-value">${fmtVal(params.symbol)}</div></div>
                <div class="param-item"><div class="param-name">基础仓位</div><div class="param-value">${fmtVal(params.base_position_eth)} ETH</div></div>
                <div class="param-item"><div class="param-name">RSI周期</div><div class="param-value">${fmtVal(params.rsi_period)}</div></div>
            </div>
        </div>
    `);

    box.innerHTML = blocks.join('');
}

// 专家建议渲染
function renderExpertAdvice(judgment) {
    const textEl = document.getElementById('expertAdviceText');
    const boxEl = document.getElementById('expertAdviceBox');
    if (!textEl || !judgment) return;
    
    textEl.textContent = judgment.text || '策略监测中...';
    // 根据建议的颜色调整边框
    const colorMap = {
        'profit': '#00f097',
        'loss': '#ff4d6a',
        'primary': '#00d4ff',
        'neutral': '#ffc107'
    };
    const color = colorMap[judgment.color] || '#ffc107';
    boxEl.style.borderColor = color;
    document.querySelector('.advice-header').style.color = color;
}

// 持仓详情渲染 (新增)
function renderPositionDetails(positions, totalMargin, currentPrice) {
    const elMargin = document.getElementById('totalMargin');
    const elEquiv = document.getElementById('equivEthSize');
    const elLong = document.getElementById('longPosSize');
    const elShort = document.getElementById('shortPosSize');
    const elAvg = document.getElementById('avgPosPrice');
    const elVal = document.getElementById('currentPosValue');

    if (!elMargin || !positions) return;

    // 格式化保证金
    elMargin.textContent = `${formatNumber(totalMargin)} USDT`;
    elMargin.style.color = totalMargin > 0 ? 'var(--profit)' : 'var(--text-primary)';

    // 提取 ETH 持仓 (假设 key 为 'ETH-USDT-SWAP' 或类似的，或者遍历)
    let netSize = 0;
    let avgPx = 0;
    
    // 遍历所有持仓 (通常只有一个 ETH 持仓)
    Object.values(positions).forEach(posList => {
        posList.forEach(p => {
            netSize += p.size;
            avgPx = p.avg_price; // 简化处理，取最后一个
        });
    });

    const absSize = Math.abs(netSize);
    elEquiv.textContent = `${absSize.toFixed(4)} ETH`;
    elLong.textContent = netSize > 0 ? `${netSize.toFixed(4)}` : '--';
    elShort.textContent = netSize < 0 ? `${Math.abs(netSize).toFixed(4)}` : '--';
    elAvg.textContent = avgPx > 0 ? formatNumber(avgPx) : '--';
    
    const curVal = absSize * (currentPrice || 0);
    elVal.textContent = curVal > 0 ? formatNumber(curVal) : '--';
}

// 交易列表渲染
function renderTradeList() {
    const container = document.getElementById('tradeList');
    const paginationDiv = document.getElementById('tradePagination');
    if (!container) return;

    const { allTrades, currentPage, pageSize } = window.tradePaginationState;
    calculateTradeStats(allTrades);

    const totalTrades = allTrades.length;
    window.tradePaginationState.totalPages = Math.ceil(totalTrades / pageSize) || 1;

    const startIdx = (window.tradePaginationState.currentPage - 1) * pageSize;
    const endIdx = Math.min(startIdx + pageSize, totalTrades);
    const pageTrades = allTrades.slice(startIdx, endIdx);

    container.innerHTML = '';

    if (pageTrades.length === 0) {
        container.innerHTML = '<div class="empty-state">暂无交易记录</div>';
        if (paginationDiv) paginationDiv.style.display = 'none';
        document.getElementById('tradeCount').textContent = '0 笔';
        return;
    }

    if (paginationDiv) paginationDiv.style.display = 'flex';

    pageTrades.forEach(trade => {
        const item = document.createElement('div');
        item.className = `trade-item ${trade.type.toLowerCase()}`;
        
        const price = parseFloat(trade.price) || 0;
        const size = parseFloat(trade.size) || 0;
        const pnl = parseFloat(trade.pnl) || 0;
        const timeStr = formatLocalTime(trade.time);
        const action = trade.action || (trade.type === 'BUY' ? '买入' : '卖出');
        
        const pnlLabel = pnl !== 0 ? (pnl > 0 ? `+${pnl.toFixed(2)} USDT` : `${pnl.toFixed(2)} USDT`) : '';
        const pnlColor = pnl > 0 ? 'var(--profit)' : (pnl < 0 ? 'var(--loss)' : 'var(--text-secondary)');

        item.innerHTML = `
            <div class="trade-icon">${action[0]}</div>
            <div class="trade-info">
                <div class="trade-type">${action} ETH <span style="font-size: 10px; font-weight: normal; opacity: 0.7;">${size.toFixed(4)} @ ${price.toFixed(2)}</span></div>
                <div class="trade-time">${timeStr}</div>
                ${trade.reason ? `<div style="font-size: 10px; color: var(--text-secondary); margin-top: 4px;">${trade.reason}</div>` : ''}
            </div>
            <div class="trade-price" style="color: ${pnlColor}">${pnlLabel || (price * size).toFixed(2) + ' U'}</div>
        `;
        container.appendChild(item);
    });

    document.getElementById('tradeCount').textContent = `${totalTrades} 笔`;
    updatePaginationUI();
}

function updatePaginationUI() {
    const info = document.getElementById('tradePageInfo');
    if (info) {
        info.textContent = `第 ${window.tradePaginationState.currentPage} / ${window.tradePaginationState.totalPages} 页`;
    }
}

function calculateTradeStats(trades) {
    let wins = 0, losses = 0, totalPnl = 0;
    let winSum = 0, lossSum = 0;

    trades.forEach(t => {
        const pnl = parseFloat(t.pnl);
        if (!isNaN(pnl) && pnl !== 0) {
            totalPnl += pnl;
            if (pnl > 0) {
                wins++;
                winSum += pnl;
            } else {
                losses++;
                lossSum += Math.abs(pnl);
            }
        }
    });

    const winRate = (wins + losses > 0) ? (wins / (wins + losses) * 100) : 0;
    const profitRatio = (losses > 0 && wins > 0) ? (winSum / wins) / (lossSum / losses) : (wins > 0 ? 99.9 : 0);

    const wrEl = document.getElementById('winRate');
    const prEl = document.getElementById('profitRatio');
    const tnEl = document.getElementById('tradeNumbers');
    const rpEl = document.getElementById('realizedPnl');

    if (wrEl) wrEl.textContent = `${winRate.toFixed(1)}%`;
    if (prEl) prEl.textContent = profitRatio.toFixed(2);
    if (tnEl) tnEl.textContent = `${wins} / ${losses}`;
    if (rpEl) {
        rpEl.textContent = `${totalPnl > 0 ? '+' : ''}${totalPnl.toFixed(2)} U`;
        rpEl.style.color = totalPnl >= 0 ? 'var(--profit)' : 'var(--loss)';
    }
}
