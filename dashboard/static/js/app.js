/**
 * 应用主入口与页面事件
 */

document.addEventListener('DOMContentLoaded', () => {
    console.log('[App] 初始化 Dashboard...');
    
    // 1. 初始化图表
    initCharts();
    
    // 2. 初始化分页
    const prevBtn = document.getElementById('prevTradePage');
    const nextBtn = document.getElementById('nextTradePage');
    
    prevBtn?.addEventListener('click', () => {
        if (window.tradePaginationState.currentPage > 1) {
            window.tradePaginationState.currentPage--;
            renderTradeList();
        }
    });
    
    nextBtn?.addEventListener('click', () => {
        if (window.tradePaginationState.currentPage < window.tradePaginationState.totalPages) {
            window.tradePaginationState.currentPage++;
            renderTradeList();
        }
    });

    // 3. 策略文档弹窗
    document.getElementById('openStrategyDocBtn')?.addEventListener('click', () => {
        document.getElementById('strategyDocModal').classList.add('show');
        renderStrategyDoc(window.latestStrategyInfo);
    });

    document.getElementById('closeStrategyDocBtn')?.addEventListener('click', () => {
        document.getElementById('strategyDocModal').classList.remove('show');
    });

    // 4. 重置确认逻辑
    const resetModal = document.getElementById('resetConfirmModal');
    document.getElementById('resetBtn')?.addEventListener('click', () => {
        resetModal.style.display = 'flex';
    });

    document.getElementById('cancelReset')?.addEventListener('click', () => {
        resetModal.style.display = 'none';
    });

    document.getElementById('confirmResetAction')?.addEventListener('click', () => {
        socket.emit('reset_strategy');
        resetModal.style.display = 'none';
    });
});
