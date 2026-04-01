/**
 * Socket 事件处理与数据分发
 */

const socket = io({ transports: ['websocket', 'polling'] });

socket.on('connect', () => {
    document.getElementById('statusDot').className = 'status-dot connected';
    document.getElementById('statusText').textContent = '已连接';
});

socket.on('disconnect', () => {
    document.getElementById('statusDot').className = 'status-dot disconnected';
    document.getElementById('statusText').textContent = '已断开';
});

socket.on('update', (data) => {
    handleDataUpdate(data);
});

socket.on('reset_ui', () => {
    if (window.isChartReady) {
        // 执行清空逻辑
        location.reload(); // 极简重置：刷新页面
    }
});

function handleDataUpdate(data) {
    // 强制更新连接状态 (作为 fallback，防止 connect 事件没触发 DOM)
    try {
        const statusText = document.getElementById('statusText');
        const statusDot = document.getElementById('statusDot');
        if (statusText && statusText.textContent === '连接中...') {
            statusText.textContent = '已连接';
            if (statusDot) statusDot.className = 'status-dot connected';
        }
    } catch (e) { console.error("Update connection status failed:", e); }

    // 0. 更新基础元数据
    try {
        if (data.uid && document.getElementById('accountUid')) {
            document.getElementById('accountUid').textContent = 'UID: ' + data.uid;
        }
    } catch (e) { console.error("Update UID failed:", e); }

    // 1. 更新资产数值与收益率
    try {
        if (data.initial_balance !== undefined && data.initial_balance !== null) {
            window.initialBalance = parseFloat(data.initial_balance);
        }

        if (data.total_value !== undefined) {
            const tvValue = parseFloat(data.total_value);
            const tv = document.getElementById('totalValue');
            const pr = document.getElementById('pnlRate');
            if (tv) tv.textContent = formatNumber(tvValue);
            
            renderAssetSummary(data);

            let pnlPct = data.pnl_pct;
            const initial = window.initialBalance || parseFloat(data.initial_balance);
            if ((pnlPct === undefined || pnlPct === null) && !isNaN(initial) && initial > 0) {
                pnlPct = (tvValue - initial) / initial * 100;
            }

            if (pnlPct !== undefined && pnlPct !== null && pr) {
                const pnl = parseFloat(pnlPct);
                pr.textContent = (isNaN(pnl) ? '--' : (pnl >= 0 ? '+' : '') + pnl.toFixed(2) + '%');
                const color = pnl >= 0 ? 'var(--profit)' : 'var(--loss)';
                if (tv) tv.style.color = color;
                pr.style.color = color;
            }
        }

        if (data.account_cash !== undefined && document.getElementById('cashValue')) {
            document.getElementById('cashValue').textContent = formatNumber(data.account_cash);
        }
    } catch (e) { console.error("Update asset summary failed:", e); }

    // 2. 更新图表 (K线, RSI, 资产曲线)
    try {
        if (data.history_candles) {
            updateChart(data.history_candles);
            if (data.history_rsi) updateRSIHistory(data.history_rsi, data.history_candles);
            if (data.history_equity) updateEquityHistory(data.history_equity);
        } else if (data.candle) {
            updateChart(data.candle);
        }

        if (data.rsi !== undefined && data.candle) {
            updateRSI(data.rsi, data.candle.t);
        }

        if (data.history_equity) {
            updateEquityHistory(data.history_equity);
        } else if (data.total_value !== undefined && data.candle) {
            updateEquity(data.total_value, data.candle.t);
        }
    } catch (e) { console.error("Update charts failed:", e); }

    // 3. 更新交易记录与标记 (V1.2 引入 try-catch 屏蔽)
    try {
        let tradeChanged = false;
        if (data.trade_history) {
            const trades = data.trade_history.slice().reverse();
            window.tradePaginationState.allTrades = trades;
            tradeChanged = true;
        } else if (data.trade) {
            const t = data.trade;
            const trades = window.tradePaginationState.allTrades;
            const ord_id = t.meta && t.meta.ord_id;
            const isDup = ord_id && trades.some(d => d.meta && d.meta.ord_id === ord_id);
            if (!isDup) {
                trades.unshift(t);
                if (trades.length > 500) trades.pop();
                tradeChanged = true;
            }
        }

        if (tradeChanged) {
            renderTradeList();
        }
        
        // 标记补丁
        if (window.tradePaginationState.allTrades && window.tradePaginationState.allTrades.length > 0) {
            updateTradeMarkers(window.tradePaginationState.allTrades.slice().reverse()); 
        }
    } catch (e) { console.error("Update trade history/markers failed:", e); }

    // 4. 更新持仓详情
    try {
        if (data.positions !== undefined) {
            renderPositionDetails(data.positions, data.total_margin || 0, data.price);
        }
    } catch (e) { console.error("Update position details failed:", e); }

    // 5. 更新策略状态 (V95)
    try {
        if (data.strategy) {
            window.latestStrategyInfo = data.strategy;
            const s = data.strategy;
            if (s.grid_range && document.getElementById('gridRange')) {
                document.getElementById('gridRange').textContent = `${s.grid_range[0].toFixed(2)} - ${s.grid_range[1].toFixed(2)}`;
            }
            if (s.grid_prices) {
                updatePriceLines(s.grid_prices);
            }
            if (s.judgment) {
                renderExpertAdvice(s.judgment);
            }
            if (s.rsi_thresholds && document.querySelector('.stat-value-c')) {
                const rsiVals = document.querySelectorAll('.stat-value-c');
                if (rsiVals.length >= 2) {
                    rsiVals[0].textContent = `${s.rsi_thresholds.oversold} / ${s.rsi_thresholds.overbought}`;
                    rsiVals[1].textContent = `${s.daily_reset || 0}/2 | ${s.breakout_reset > 0 ? '突破' : '正常'}`;
                }
            }
        }
    } catch (e) { console.error("Update strategy status failed:", e); }
}
