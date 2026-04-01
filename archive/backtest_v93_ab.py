import eventlet
eventlet.monkey_patch()

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional

CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import requests

from core.types import MarketData, Position, Side
from strategies.eth_swap_v93 import V93Strategy, logger as strategy_logger
from strategies.eth_swap_v93_candidate import V93CandidateStrategy

strategy_logger.handlers = []
strategy_logger.propagate = False
strategy_logger.disabled = True


OKX_URL = "https://www.okx.com/api/v5/market/history-candles"
SYMBOL = "ETH-USDT-SWAP"
BAR = "1m"


@dataclass
class RoundTrip:
    side: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    size: float
    pnl: float


class Context(SimpleNamespace):
    pass


class ProposedV93Strategy(V93Strategy):
    """
    试验版：
    1. 层只代表区域，不再限制“0层下半区才能多 / 2层上半区才能空”
    2. 趋势不再决定是否持仓，只轻度调节仓位权重
    3. 保持均值回归骨架
    """

    def _update_dynamic_rsi_thresholds(self, atr: float, price: float, trend: int):
        # 固定阈值，避免趋势直接改写主交易逻辑
        self.rsi_oversold = 25.0
        self.rsi_overbought = 85.0
        self.rsi_exit_oversold = 40.0
        self.rsi_exit_overbought = 70.0

    def _size_by_trend(self, base_size: float, trend: int, direction: str) -> float:
        if direction == 'long':
            if trend == 1:
                return base_size * 1.25
            if trend == -1:
                return base_size * 0.75
        if direction == 'short':
            if trend == -1:
                return base_size * 1.25
            if trend == 1:
                return base_size * 0.75
        return base_size

    def _grid_trading(self, layer: int, rsi: float, has_long: bool, has_short: bool,
                      current_price: float, current_time: datetime, trend: int,
                      context) -> List:
        signals = []

        in_low_zone = layer in (-1, 0)
        in_mid_zone = layer == 1
        in_high_zone = layer in (2, 3)

        # 低估区：优先试多 / 平空
        if in_low_zone:
            if has_short and rsi <= self.rsi_exit_oversold:
                pos_size = 0.0
                for symbol, pos in context.positions.items():
                    if symbol == self.symbol and pos.size < 0:
                        pos_size = abs(pos.size)
                        break
                if pos_size > 0:
                    signals.append(self._make_signal('buy', pos_size, current_price, current_time,
                                                     f'close_short_low_zone_l{layer}', 'short'))

            elif not has_long and rsi <= self.rsi_oversold:
                size = self._size_by_trend(0.2 * self.current_leverage, trend, 'long')
                signals.append(self._make_signal('buy', size, current_price, current_time,
                                                 f'open_long_low_zone_l{layer}', 'long'))

        # 公允区：不主动开新仓，留给已有仓位自然过渡
        elif in_mid_zone:
            pass

        # 高估区：优先试空 / 平多
        elif in_high_zone:
            if has_long and rsi >= self.rsi_exit_overbought:
                pos_size = 0.0
                for symbol, pos in context.positions.items():
                    if symbol == self.symbol and pos.size > 0:
                        pos_size = pos.size
                        break
                if pos_size > 0:
                    signals.append(self._make_signal('sell', pos_size, current_price, current_time,
                                                     f'close_long_high_zone_l{layer}', 'long'))

            elif not has_short and rsi >= self.rsi_overbought:
                size = self._size_by_trend(0.2 * self.current_leverage, trend, 'short')
                signals.append(self._make_signal('sell', size, current_price, current_time,
                                                 f'open_short_high_zone_l{layer}', 'short'))

        return signals

    def _no_grid_trading(self, rsi: float, has_long: bool, has_short: bool,
                         current_price: float, current_time: datetime, trend: int,
                         context) -> List:
        # 越界时不激进反手，沿用原规则但趋势只调仓位
        signals = []
        if rsi <= self.rsi_oversold and not has_long:
            size = self._size_by_trend(0.15 * self.current_leverage, trend, 'long')
            signals.append(self._make_signal('buy', size, current_price, current_time,
                                             'no_grid_open_long', 'long'))
        elif rsi >= self.rsi_overbought and not has_short:
            size = self._size_by_trend(0.15 * self.current_leverage, trend, 'short')
            signals.append(self._make_signal('sell', size, current_price, current_time,
                                             'no_grid_open_short', 'short'))

        if has_long and rsi >= self.rsi_exit_overbought:
            for symbol, pos in context.positions.items():
                if symbol == self.symbol and pos.size > 0:
                    signals.append(self._make_signal('sell', pos.size, current_price, current_time,
                                                     'no_grid_close_long', 'long'))
                    break
        elif has_short and rsi <= self.rsi_exit_oversold:
            for symbol, pos in context.positions.items():
                if symbol == self.symbol and pos.size < 0:
                    signals.append(self._make_signal('buy', abs(pos.size), current_price, current_time,
                                                     'no_grid_close_short', 'short'))
                    break
        return signals

    def _make_signal(self, side: str, size: float, price: float, ts: datetime, reason: str, pos_side: str):
        return SimpleNamespace(
            symbol=self.symbol,
            side=Side.BUY if side == 'buy' else Side.SELL,
            size=float(size),
            price=price,
            timestamp=ts,
            meta={'reason': reason, 'posSide': pos_side},
            reason=reason,
        )


class Account:
    def __init__(self, initial_equity: float, fee_rate: float):
        self.initial_equity = initial_equity
        self.realized = 0.0
        self.fees = 0.0
        self.fee_rate = fee_rate
        self.long_size = 0.0
        self.long_avg = 0.0
        self.long_entry_time: Optional[datetime] = None
        self.short_size = 0.0
        self.short_avg = 0.0
        self.short_entry_time: Optional[datetime] = None
        self.round_trips: List[RoundTrip] = []
        self.equity_curve: List[float] = []
        self.timestamps: List[datetime] = []
        self.signal_count = 0

    def positions_dict(self) -> Dict[str, Position]:
        positions = {}
        if self.long_size > 0:
            positions[SYMBOL] = Position(symbol=SYMBOL, size=self.long_size, avg_price=self.long_avg,
                                         entry_time=self.long_entry_time or datetime.now(timezone.utc), unrealized_pnl=0.0)
        if self.short_size > 0:
            positions[SYMBOL] = Position(symbol=SYMBOL, size=-self.short_size, avg_price=self.short_avg,
                                         entry_time=self.short_entry_time or datetime.now(timezone.utc), unrealized_pnl=0.0)
        return positions

    def _close_long(self, price: float, size: float, ts: datetime):
        close_size = min(size, self.long_size)
        pnl = (price - self.long_avg) * close_size
        fee = price * close_size * self.fee_rate
        self.realized += pnl - fee
        self.fees += fee
        self.round_trips.append(RoundTrip('long', self.long_entry_time or ts, ts, self.long_avg, price, close_size, pnl - fee))
        self.long_size -= close_size
        if self.long_size <= 1e-12:
            self.long_size = 0.0
            self.long_avg = 0.0
            self.long_entry_time = None

    def _open_long(self, price: float, size: float, ts: datetime):
        fee = price * size * self.fee_rate
        self.fees += fee
        self.realized -= fee
        total_cost = self.long_avg * self.long_size + price * size
        self.long_size += size
        self.long_avg = total_cost / self.long_size
        if self.long_entry_time is None:
            self.long_entry_time = ts

    def _close_short(self, price: float, size: float, ts: datetime):
        close_size = min(size, self.short_size)
        pnl = (self.short_avg - price) * close_size
        fee = price * close_size * self.fee_rate
        self.realized += pnl - fee
        self.fees += fee
        self.round_trips.append(RoundTrip('short', self.short_entry_time or ts, ts, self.short_avg, price, close_size, pnl - fee))
        self.short_size -= close_size
        if self.short_size <= 1e-12:
            self.short_size = 0.0
            self.short_avg = 0.0
            self.short_entry_time = None

    def _open_short(self, price: float, size: float, ts: datetime):
        fee = price * size * self.fee_rate
        self.fees += fee
        self.realized -= fee
        total_cost = self.short_avg * self.short_size + price * size
        self.short_size += size
        self.short_avg = total_cost / self.short_size
        if self.short_entry_time is None:
            self.short_entry_time = ts

    def apply_signal(self, signal, price: float, ts: datetime):
        self.signal_count += 1
        pos_side = signal.meta.get('posSide')
        size = float(signal.size)
        if pos_side == 'long':
            if signal.side == Side.BUY:
                self._open_long(price, size, ts)
            else:
                self._close_long(price, size, ts)
        elif pos_side == 'short':
            if signal.side == Side.SELL:
                self._open_short(price, size, ts)
            else:
                self._close_short(price, size, ts)

    def mark_equity(self, price: float, ts: datetime):
        unreal = 0.0
        if self.long_size > 0:
            unreal += (price - self.long_avg) * self.long_size
        if self.short_size > 0:
            unreal += (self.short_avg - price) * self.short_size
        equity = self.initial_equity + self.realized + unreal
        self.timestamps.append(ts)
        self.equity_curve.append(equity)
        return equity

    def summary(self):
        if not self.equity_curve:
            return {}
        equity = pd.Series(self.equity_curve, index=pd.to_datetime(self.timestamps, utc=True))
        ret = equity.pct_change().fillna(0)
        total_return = equity.iloc[-1] / equity.iloc[0] - 1
        dd = (equity / equity.cummax() - 1).min()
        wins = [t for t in self.round_trips if t.pnl > 0]
        losses = [t for t in self.round_trips if t.pnl <= 0]
        profit_factor = (sum(t.pnl for t in wins) / abs(sum(t.pnl for t in losses))) if losses and sum(t.pnl for t in losses) != 0 else math.inf
        return {
            'initial_equity': round(float(equity.iloc[0]), 2),
            'final_equity': round(float(equity.iloc[-1]), 2),
            'total_return_pct': round(total_return * 100, 2),
            'max_drawdown_pct': round(float(dd) * 100, 2),
            'trades': len(self.round_trips),
            'win_rate_pct': round((len(wins) / len(self.round_trips) * 100), 2) if self.round_trips else 0.0,
            'profit_factor': round(float(profit_factor), 3) if math.isfinite(profit_factor) else None,
            'fees': round(self.fees, 2),
            'signals': self.signal_count,
            'long_trades': len([t for t in self.round_trips if t.side == 'long']),
            'short_trades': len([t for t in self.round_trips if t.side == 'short']),
        }


def fetch_okx_history(symbol: str, bar: str, start: datetime, end: datetime) -> pd.DataFrame:
    rows = []
    after = None
    session = requests.Session()
    batch_count = 0
    while True:
        params = {'instId': symbol, 'bar': bar, 'limit': '100'}
        if after is not None:
            params['after'] = str(after)
        resp = session.get(OKX_URL, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json().get('data', [])
        if not data:
            break
        rows.extend(data)
        batch_count += 1
        last_ts = int(data[-1][0])
        oldest_dt = datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc)
        if batch_count % 50 == 0:
            print(f'Fetched batches: {batch_count}, oldest={oldest_dt.isoformat()}, rows={len(rows)}')
        if oldest_dt <= start:
            break
        after = last_ts

    df = pd.DataFrame(rows, columns=['ts', 'open', 'high', 'low', 'close', 'volume', 'volCcy', 'volCcyQuote', 'confirm'])
    df['ts'] = pd.to_datetime(df['ts'].astype('int64'), unit='ms', utc=True)
    df = df.drop_duplicates(subset=['ts']).sort_values('ts')
    df = df[(df['ts'] >= start) & (df['ts'] <= end)].copy()
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = df[col].astype(float)
    df = df.set_index('ts')[['open', 'high', 'low', 'close', 'volume']]
    return df


def run_strategy(df: pd.DataFrame, strategy_cls, label: str, initial_equity: float = 5000.0, fee_rate: float = 0.0005):
    strategy = strategy_cls(symbol=SYMBOL, leverage_base=3)
    account = Account(initial_equity=initial_equity, fee_rate=fee_rate)
    total = len(df)

    for idx, (ts, row) in enumerate(df.iterrows(), start=1):
        data = MarketData(
            symbol=SYMBOL,
            timestamp=ts.to_pydatetime(),
            open=float(row['open']),
            high=float(row['high']),
            low=float(row['low']),
            close=float(row['close']),
            volume=float(row['volume'])
        )
        pos = {}
        # 手动维护同一 symbol 多空仓；策略内部仅读 has_long/has_short 与 size 符号
        if account.long_size > 0 and account.short_size > 0:
            # 若双向都存在，优先给一个聚合视图让 strategy 看到双向中的一个会丢失；
            # 这里采用最近优先原则：分两次回放（先多后空）不可行，因此简单兼容为净额视图。
            net_size = account.long_size - account.short_size
            avg_price = account.long_avg if net_size >= 0 else account.short_avg
            pos[SYMBOL] = Position(symbol=SYMBOL, size=net_size, avg_price=avg_price,
                                   entry_time=ts.to_pydatetime(), unrealized_pnl=0.0)
        elif account.long_size > 0:
            pos[SYMBOL] = Position(symbol=SYMBOL, size=account.long_size, avg_price=account.long_avg,
                                   entry_time=account.long_entry_time or ts.to_pydatetime(), unrealized_pnl=0.0)
        elif account.short_size > 0:
            pos[SYMBOL] = Position(symbol=SYMBOL, size=-account.short_size, avg_price=account.short_avg,
                                   entry_time=account.short_entry_time or ts.to_pydatetime(), unrealized_pnl=0.0)

        context = Context(timestamp=ts.to_pydatetime(), cash=account.initial_equity + account.realized,
                          positions=pos, current_prices={SYMBOL: float(row['close'])}, meta={})
        signals = strategy.on_data(data, context)
        for signal in signals:
            account.apply_signal(signal, float(row['close']), ts.to_pydatetime())
        account.mark_equity(float(row['close']), ts.to_pydatetime())
        if idx % 5000 == 0:
            print(f'[{label}] progress: {idx}/{total}, price={row["close"]:.2f}, trades={len(account.round_trips)}')

    return {'label': label, 'summary': account.summary(), 'round_trips': [t.__dict__ for t in account.round_trips]}


def main():
    parser = argparse.ArgumentParser(description='ETH V93 A/B backtest')
    parser.add_argument('--days', type=int, default=30)
    parser.add_argument('--initial-equity', type=float, default=5000.0)
    parser.add_argument('--fee-rate', type=float, default=0.0005)
    parser.add_argument('--output', type=str, default=None)
    args = parser.parse_args()

    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(days=args.days)

    print(f'Fetching {SYMBOL} {BAR} data from {start.isoformat()} to {end.isoformat()} ...')
    df = fetch_okx_history(SYMBOL, BAR, start, end)
    if df.empty:
        raise SystemExit('No data fetched')
    print(f'Fetched {len(df)} bars')

    base = run_strategy(df, V93Strategy, 'current_v93', args.initial_equity, args.fee_rate)
    proposed = run_strategy(df, V93CandidateStrategy, 'proposed_v93', args.initial_equity, args.fee_rate)

    report = {
        'data': {
            'symbol': SYMBOL,
            'bar': BAR,
            'start': start.isoformat(),
            'end': end.isoformat(),
            'bars': int(len(df)),
        },
        'results': [base, proposed],
    }

    print('\n=== SUMMARY ===')
    for item in report['results']:
        print(f"\n[{item['label']}]")
        for k, v in item['summary'].items():
            print(f"- {k}: {v}")

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'\nSaved report to {output_path}')


if __name__ == '__main__':
    main()
