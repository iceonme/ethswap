/**
 * TradingView 图表控制模块
 */

window.isChartReady = false;
window.candleDataBuffer = [];
window.rsiDataBuffer = [];
window.equityDataBuffer = [];
window.gridPriceLines = []; // 存储价格线实例
window.pendingTrades = null; // 缓冲未就绪时的交易记录

let mainChart, rsiChart, equityChart;
let candleSeries, rsiSeries, equitySeries;

/**
 * 绘制网格价格线 (VH, P3, P2, P1, P0, VL)
 * @param {Array} prices 价格数组
 */
function updatePriceLines(prices) {
    if (!window.isChartReady || !candleSeries || !Array.isArray(prices)) return;

    // 1. 清除旧线
    window.gridPriceLines.forEach(line => candleSeries.removePriceLine(line));
    window.gridPriceLines = [];

    prices.forEach((price, index) => {
        if (!price) return;

        const isVirtual = (index === 0 || index === 5); // VH 和 VL 是虚拟层
        // 定义层级颜色：极高(橙红), 实体(青色), 极低(亮绿)
        let color = 'rgba(0, 212, 255, 0.8)';
        if (index === 0) color = '#ff4d6a'; // VH
        if (index === 5) color = '#00f097'; // VL
        if (isVirtual) color = color.replace(')', ', 0.5)').replace('#', 'rgba('); // 稍微透明点，如果是 hex 需要转换，这里简化处理

        const lineStyle = isVirtual ? LightweightCharts.LineStyle.Dashed : LightweightCharts.LineStyle.Solid;
        const title = isVirtual ? (index === 0 ? '极高位' : '极低位') : 
                      (index === 1 ? '高层顶' : (index === 2 ? '中轴' : (index === 3 ? '底层顶' : '底层底')));

        const priceLine = candleSeries.createPriceLine({
            price: parseFloat(price),
            color: isVirtual ? (index === 0 ? '#ff4d6a' : '#00f097') : '#00d4ff',
            lineWidth: isVirtual ? 1 : 2,
            lineStyle: lineStyle,
            axisLabelVisible: true,
            title: title,
        });

        window.gridPriceLines.push(priceLine);
    });
}

function convertTime(timestamp, alignMinutes = false) {
    if (!timestamp) return null;
    let ms = typeof timestamp === 'number' ? (timestamp > 1e11 ? timestamp : timestamp * 1000) : new Date(timestamp).getTime();
    if (isNaN(ms)) return null;
    let seconds = Math.floor(ms / 1000);
    // 强制增加 8 小时以对齐北京时间显示 (UTC -> UTC+8)
    return seconds + (8 * 3600); 
}

function initCharts() {
    const chartOptions = {
        layout: { background: { color: 'transparent' }, textColor: '#94a3b8' },
        grid: { vertLines: { color: 'rgba(255,255,255,0.03)' }, horzLines: { color: 'rgba(255,255,255,0.03)' } },
        timeScale: { borderColor: 'rgba(255,255,255,0.1)', timeVisible: true }
    };

    mainChart = LightweightCharts.createChart(document.getElementById('tv-chart-main'), chartOptions);
    candleSeries = mainChart.addCandlestickSeries({ upColor: '#00d084', downColor: '#ff4757' });

    rsiChart = LightweightCharts.createChart(document.getElementById('tv-chart-rsi'), { ...chartOptions, timeScale: { visible: false } });
    rsiSeries = rsiChart.addLineSeries({ color: '#a855f7', lineWidth: 2 });
    
    // 添加 RSI 70/30 警戒线
    rsiSeries.createPriceLine({
        price: 70,
        color: 'rgba(255, 77, 106, 0.4)',
        lineWidth: 1,
        lineStyle: LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: true,
        title: '超买(70)',
    });
    rsiSeries.createPriceLine({
        price: 30,
        color: 'rgba(0, 240, 151, 0.4)',
        lineWidth: 1,
        lineStyle: LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: true,
        title: '超卖(30)',
    });

    equityChart = LightweightCharts.createChart(document.getElementById('tv-chart-equity'), { ...chartOptions, timeScale: { visible: false } });
    equitySeries = equityChart.addAreaSeries({ lineColor: '#fbbf24', topColor: 'rgba(251,191,36,0.4)', bottomColor: 'rgba(251,191,36,0)' });

    // --- 图表联动逻辑 (Synchronization) ---
    const charts = [mainChart, rsiChart, equityChart];
    let isSyncing = false;

    charts.forEach((chart, index) => {
        // 1. 时间轴同步 (Visible Range)
        chart.timeScale().subscribeVisibleTimeRangeChange((range) => {
            if (isSyncing || !range || range.from === null || range.to === null) return;
            isSyncing = true;
            charts.forEach((otherChart, otherIndex) => {
                if (index !== otherIndex) {
                    try {
                        otherChart.timeScale().setVisibleRange(range);
                    } catch (e) {
                        // 静默处理：可能其它图表尚未完全就绪
                    }
                }
            });
            isSyncing = false;
        });

        // 2. 十字光标同步 (Crosshair)
        chart.subscribeCrosshairMove((param) => {
            if (isSyncing) return;
            isSyncing = true;
            try {
                charts.forEach((c, i) => {
                    if (index !== i) {
                        try {
                            if (!param || !param.time || (param.point && param.point.x < 0)) {
                                c.setCrosshairPosition(undefined, undefined, undefined);
                            } else {
                                const s = i === 0 ? candleSeries : (i === 1 ? rsiSeries : equitySeries);
                                if (s) {
                                    // 仅同步垂直线：在某些版本中，需通过传递 undefined 来隐藏价格线，或传一个 0。
                                    // 为保证百分百生效，直接传 0，但增加 try-catch 保护
                                    c.setCrosshairPosition(0, param.time, s);
                                }
                            }
                        } catch (innerErr) {
                            // console.warn("[Charts] Crosshair sync detail failed", innerErr);
                        }
                    }
                });
            } catch (e) {}
            isSyncing = false;
        });
    });

    window.isChartReady = true;
    
    // 如果有缓冲的交易记录，立即补绘
    if (window.pendingTrades) {
        // console.log("[Charts] 补绘缓冲的交易标记");
        updateTradeMarkers(window.pendingTrades);
        window.pendingTrades = null;
    }
}

function updateChart(data) {
    if (!window.isChartReady) return;
    const candles = Array.isArray(data) ? data : [data];
    const tvData = candles.map(c => ({
        time: convertTime(c.t),
        open: parseFloat(c.o),
        high: parseFloat(c.h),
        low: parseFloat(c.l),
        close: parseFloat(c.c)
    })).filter(c => c.time && !isNaN(c.close));

    if (tvData.length > 1) {
        candleSeries.setData(tvData);
        // 如果是全量历史更新，尝试自适应视野
        if (tvData.length > 50) {
            mainChart.timeScale().fitContent();
        }
    } else if (tvData.length === 1) {
        candleSeries.update(tvData[0]);
    }
}

/**
 * 在 K 线图上绘制交易标记 (Buy/Sell)
 * @param {Array} trades 交易记录
 */
function updateTradeMarkers(trades) {
    if (!trades || !Array.isArray(trades)) return;
    
    if (!window.isChartReady || !candleSeries) {
        // console.log("[Charts] 图表尚未就绪，暂存交易标记");
        window.pendingTrades = trades;
        return;
    }

    const markers = trades.map(t => {
        const time = convertTime(t.t);
        if (!time) return null;

        // 兼容处理：买入/开多/BUY 均视为绿色买入，卖出/平多/SELL 均视为红色卖出
        const act = (t.action || t.type || "").toUpperCase();
        const isBuy = act.includes('买') || act.includes('开多') || act.includes('BUY');
        const isSell = act.includes('卖') || act.includes('平多') || act.includes('SELL');

        if (!isBuy && !isSell) return null;

        return {
            time: time,
            position: isBuy ? 'belowBar' : 'aboveBar',
            color: isBuy ? '#00f097' : '#ff4d6a',
            shape: isBuy ? 'arrowUp' : 'arrowDown',
            text: isBuy ? '买入' : '卖出',
            size: 2 // 稍微调大点
        };
    }).filter(m => m !== null);

    console.log(`[Charts] 生成了 ${markers.length} 个买卖标记`);

    // 按时间排序，Lightweight Charts 要求 markers 必须按时间升序
    markers.sort((a, b) => a.time - b.time);
    
    // 简单的去重逻辑：同一秒内只保留一个标记，防止重叠
    const uniqueMarkers = [];
    const seenTimes = new Set();
    markers.forEach(m => {
        if (!seenTimes.has(m.time)) {
            uniqueMarkers.push(m);
            seenTimes.add(m.time);
        }
    });

    // console.log(`[Charts] Drawing ${uniqueMarkers.length} markers`);
    candleSeries.setMarkers(uniqueMarkers);
}

function updateRSI(val, ts) {
    const time = convertTime(ts);
    if (time && rsiSeries) rsiSeries.update({ time, value: parseFloat(val) });
}

function updateRSIHistory(history, candles) {
    if (!window.isChartReady || !rsiSeries || !Array.isArray(history) || !Array.isArray(candles)) return;
    
    // RSI 历史对齐逻辑：使用 K 线的时间戳
    const tvData = [];
    const minLen = Math.min(history.length, candles.length);
    const candleSubset = candles.slice(-minLen);
    const rsiSubset = history.slice(-minLen);
    
    for (let i = 0; i < minLen; i++) {
        const time = convertTime(candleSubset[i].t);
        if (time) {
            tvData.push({ time, value: parseFloat(rsiSubset[i]) });
        }
    }
    
    if (tvData.length > 0) {
        rsiSeries.setData(tvData);
    }
}

function updateEquity(val, ts) {
    const time = convertTime(ts);
    if (time && equitySeries) equitySeries.update({ time, value: parseFloat(val) });
}

function updateEquityHistory(history) {
    if (!window.isChartReady || !equitySeries || !Array.isArray(history)) return;
    const tvData = history.map(h => ({
        time: convertTime(h.t),
        value: parseFloat(h.v)
    })).filter(h => h.time && !isNaN(h.value));
    
    if (tvData.length > 0) {
        equitySeries.setData(tvData);
    }
}
