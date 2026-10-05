"""
Демо-торговый модуль для тестирования стратегии возврата к среднему.
Позволяет эмулировать исполнение сделок на исторических/реальных данных,
используя сигналы от процесса Орнштейна-Уленбека (OU).

Поддерживает два режима исполнения входа:
  - TAKER (рыночный ордер, мгновенное исполнение, комиссия 0.06%);
  - MAKER (лимитный ордер, может исполниться не сразу, комиссия 0.02%).

Выход всегда исполняется как TAKER (упрощение для первой итерации).

Логика выхода (проверяется в порядке приоритета):
  1. HARD STOP   — |z| >= stop_z
  2. EXIT TARGET — |z| <= exit_z
  3. TRAILING    — защита прибыли, если |z| откатился от лучшего значения
  4. TIME STOP   — жёсткий лимит по времени удержания

Баланс виртуальный, сделки не отправляются на биржу.
"""

import json
from pathlib import Path
from typing import Optional, List, Dict

from dataclasses import dataclass
from threading import RLock


@dataclass
class Trade:
    """Одна завершённая сделка."""
    direction: str          # 'BUY' или 'SELL'
    entry_price: float
    exit_price: float
    entry_time: int         # timestamp открытия (мс)
    exit_time: int          # timestamp закрытия
    amount: float           # количество базовой валюты
    pnl: float              # прибыль/убыток в USDT
    fee: float              # комиссия в USDT
    reason: str             # 'exit' | 'stop' | 'trailing_stop' | 'time_stop'

    # --- поля maker/taker (с дефолтами для совместимости со старыми state-файлами) ---
    is_maker_entry: bool = False
    entry_fee_rate: float = 0.0006
    exit_fee_rate: float = 0.0006

    # --- поля для анализа trailing/time ---
    best_z_abs: Optional[float] = None    # минимальный |z|, достигнутый в позиции
    hold_minutes: Optional[float] = None  # сколько минут держали позицию
    best_price: Optional[float] = None    # лучшая цена позиции (для анализа)


class DemoTrader:
    """
    Эмулятор торговли с использованием сигналов OU.

    Логика входа:
      - При появлении сигнала |z| >= entry_z выставляется лимитный ордер
        (если maker_enabled=True) на отступе maker_offset от close свечи.
      - Ордер живёт до maker_timeout_candles свечей. Исполняется, если цена
        коснётся лимита (low <= limit для BUY, high >= limit для SELL).
      - Если за отведённое время не исполнен — отменяется, сигнал пропускается.

    Логика выхода (по порядку приоритета):
      1. HARD STOP   — |z| >= stop_z (катастрофическая защита)
      2. EXIT TARGET — |z| <= exit_z (достигли цели)
      3. TRAILING    — |z| был близок к 0 и откатился на trailing_reversal_z
      4. TIME STOP   — позиция открыта дольше max_position_minutes
    """

    def __init__(self,
                 initial_balance: float = 10000.0,
                 position_size_pct: float = 0.1,
                 fee_rate: float = 0.0006,
                 slippage: float = 0.0001,
                 entry_z: float = 2.0,
                 exit_z: float = 0.5,
                 stop_z: float = 3.5,
                 leverage: float = 1.0,
                 # --- maker/taker ---
                 fee_rate_taker: Optional[float] = None,
                 fee_rate_maker: float = 0.0002,
                 maker_enabled: bool = True,
                 maker_offset: float = 0.0002,
                 maker_timeout_candles: int = 3,
                 # --- time stop ---
                 max_position_minutes: float = 20.0,
                 # --- trailing stop по z ---
                 trailing_activation_z: float = 0.8,
                 trailing_reversal_z: float = 0.5,
                 # --- стопы по цене ---
                 price_stop_pct: float = 0.005,        # hard stop по цене
                 price_trailing_pct: float = 0.004,    # trailing по цене
                 # --- state ---
                 state_file: str = "demo_trader_state.json"):

        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.position_size_pct = position_size_pct

        # Комиссии
        self.fee_rate_taker = fee_rate_taker if fee_rate_taker is not None else fee_rate
        self.fee_rate_maker = fee_rate_maker
        self.fee_rate = self.fee_rate_taker

        self.slippage = slippage
        self.entry_z = entry_z
        self.exit_z = exit_z
        self.stop_z = stop_z
        self.leverage = leverage

        # Maker
        self.maker_enabled = maker_enabled
        self.maker_offset = maker_offset
        self.maker_timeout_candles = maker_timeout_candles

        # Time-stop (0 = отключено)
        self.max_position_minutes = max_position_minutes

        # Trailing-stop по z
        self.trailing_activation_z = trailing_activation_z
        self.trailing_reversal_z = trailing_reversal_z

        # Стопы по цене
        self.price_stop_pct = price_stop_pct
        self.price_trailing_pct = price_trailing_pct

        # Состояние
        self.position: Optional[Dict] = None
        self.pending_order: Optional[Dict] = None
        self.trades: List[Trade] = []

        self.total_pnl = 0.0
        self.total_fees = 0.0
        self.win_count = 0
        self.loss_count = 0

        self._lock = RLock()
        self.state_file = state_file
        self.load_state()

    # ---------- Управление ----------

    def reset(self):
        """Сброс всех параметров к начальным."""
        with self._lock:
            self.balance = self.initial_balance
            self.position = None
            self.pending_order = None
            self.trades.clear()
            self.total_pnl = 0.0
            self.total_fees = 0.0
            self.win_count = 0
            self.loss_count = 0
            self.save_state()

    def set_leverage(self, leverage: float):
        """Устанавливает кредитное плечо и сохраняет состояние."""
        with self._lock:
            leverage = max(1.0, min(10.0, float(leverage)))
            self.leverage = leverage
            self.save_state()

    # ---------- Основной цикл ----------

    def update(self, candle, ou_status: Dict, timestamp: int) -> None:
        """
        Вызывается на каждой закрытой свече.

        Args:
            candle:    dict со свечой {open, high, low, close, volume}
                       ИЛИ float (цена закрытия) — legacy-режим (maker отключается).
            ou_status: результат OUMeanReversion.update()
            timestamp: время закрытия свечи (мс)
        """
        with self._lock:
            if not ou_status.get('ready', False):
                return

            is_full_candle = isinstance(candle, dict)
            if is_full_candle:
                candle_dict = candle
            else:
                p = float(candle)
                candle_dict = {'open': p, 'high': p, 'low': p, 'close': p, 'volume': 0}

            use_maker = self.maker_enabled and is_full_candle

            # 1. Обработка висящего лимитного ордера
            if self.pending_order is not None:
                if self._try_fill_pending(candle_dict, timestamp, ou_status):
                    return  # позиция только что открылась

                self.pending_order['candles_waited'] += 1
                if self.pending_order['candles_waited'] >= self.maker_timeout_candles:
                    self._cancel_pending(reason='timeout')

                # Если pending существует — больше на этой свече ничего не делаем.
                # Новый сигнал будет проигнорирован, пока не истечёт таймаут или не исполнится.
                return

            # 2. Управление открытой позицией
            if self.position is not None:
                self._manage_position(candle_dict, ou_status, timestamp)
                return

            # 3. Попытка выставить новый вход
            self._try_place_entry(candle_dict, ou_status, timestamp, use_maker=use_maker)

    # ---------- Управление позицией (выходы) ----------

    def _manage_position(self, candle: Dict, ou_status: Dict, timestamp: int):
        """
        Проверяет условия выхода в порядке приоритета:
          0. Ликвидация (по high/low)
          1. Hard stop по цене (по high/low)
          2. Trailing stop по цене (по high/low)
          3. Hard stop по z (по close)
          4. Exit target по z (по close)
          5. Trailing stop по z (по close)
          6. Time stop
        """
        z = ou_status.get('z', 0.0)
        abs_z = abs(z)

        close_price = candle['close']
        high_price = candle['high']
        low_price = candle['low']

        direction = self.position['direction']
        entry_price = self.position['entry_price']
        amount = self.position['amount']
        margin_used = self.position.get('margin_used', 0.0)

        # --- 0. Ликвидация (по high/low) ---
        if margin_used > 0 and self.leverage > 1:
            if direction == 'BUY':
                liq_price = entry_price - margin_used / amount
                if low_price <= liq_price:
                    self._liquidate(timestamp, liq_price, margin_used)
                    return
            else:  # SELL
                liq_price = entry_price + margin_used / amount
                if high_price >= liq_price:
                    self._liquidate(timestamp, liq_price, margin_used)
                    return

        # --- 1. Hard stop по цене (по high/low) ---
        if self.price_stop_pct > 0:
            if direction == 'BUY':
                stop_level = entry_price * (1 - self.price_stop_pct)
                if low_price <= stop_level:
                    print(f"🛑 [PRICE_STOP] BUY low={low_price:.2f} <= {stop_level:.2f}")
                    self._close_position(stop_level, timestamp, reason='price_stop')
                    return
            else:  # SELL
                stop_level = entry_price * (1 + self.price_stop_pct)
                if high_price >= stop_level:
                    print(f"🛑 [PRICE_STOP] SELL high={high_price:.2f} >= {stop_level:.2f}")
                    self._close_position(stop_level, timestamp, reason='price_stop')
                    return

        # --- 2. Trailing stop по цене (по high/low) ---
        if self.price_trailing_pct > 0:
            best_price = self.position.get('best_price', entry_price)

            # Обновляем лучшую цену по экстремуму свечи в нашу сторону
            if direction == 'BUY':
                new_best = max(best_price, high_price)
            else:
                new_best = min(best_price, low_price)

            self.position['best_price'] = new_best

            # Trailing активирован, если best_price сдвинулся от entry в нашу сторону
            trailing_activated = (
                (direction == 'BUY' and new_best > entry_price) or
                (direction == 'SELL' and new_best < entry_price)
            )

            if trailing_activated:
                if direction == 'BUY':
                    trigger_level = new_best * (1 - self.price_trailing_pct)
                    if low_price <= trigger_level:
                        print(f"📉 [PRICE_TRAILING] BUY low={low_price:.2f} <= {trigger_level:.2f} "
                              f"(best={new_best:.2f})")
                        self._close_position(trigger_level, timestamp, reason='price_trailing')
                        return
                else:  # SELL
                    trigger_level = new_best * (1 + self.price_trailing_pct)
                    if high_price >= trigger_level:
                        print(f"📉 [PRICE_TRAILING] SELL high={high_price:.2f} >= {trigger_level:.2f} "
                              f"(best={new_best:.2f})")
                        self._close_position(trigger_level, timestamp, reason='price_trailing')
                        return

        # --- 3. Hard stop по z (по close) ---
        if abs_z >= self.stop_z:
            self._close_position(close_price, timestamp, reason='stop')
            return

        # --- Обновляем best_z_abs ---
        prev_best = self.position.get('best_z_abs')
        if prev_best is None:
            prev_best = float('inf')
        new_best_z = min(prev_best, abs_z)
        self.position['best_z_abs'] = new_best_z

        # --- 4. Exit target по z (по close) ---
        if abs_z <= self.exit_z:
            self._close_position(close_price, timestamp, reason='exit')
            return

        # --- 5. Trailing stop по z (по close) ---
        if new_best_z <= self.trailing_activation_z:
            if abs_z > new_best_z + self.trailing_reversal_z:
                self._close_position(close_price, timestamp, reason='trailing_stop')
                return

        # --- 6. Time stop ---
        if self.max_position_minutes > 0:
            elapsed_min = (timestamp - self.position['entry_time']) / 1000.0 / 60.0
            if elapsed_min >= self.max_position_minutes:
                self._close_position(close_price, timestamp, reason='time_stop')
                return

    def _try_place_entry(self, candle: Dict, ou_status: Dict, timestamp: int, use_maker: bool):
        """Проверяет сигнал и, если он есть, выставляет вход (maker или taker)."""
        z = ou_status.get('z', 0.0)
        signal = ou_status.get('signal', 'FLAT')

        if z <= -self.entry_z and signal == 'BUY':
            self._place_entry('BUY', candle, timestamp, use_maker=use_maker, z=z)
        elif z >= self.entry_z and signal == 'SELL':
            self._place_entry('SELL', candle, timestamp, use_maker=use_maker, z=z)

    def _place_entry(self, direction: str, candle: Dict, timestamp: int,
                     use_maker: bool, z: float):
        """Размещает вход: лимитный ордер (maker) или мгновенное исполнение (taker)."""
        base_price = candle['close']

        if not use_maker:
            self._open_position(direction, base_price, timestamp, is_maker=False, entry_z=z)
            return

        if direction == 'BUY':
            limit_price = base_price * (1 - self.maker_offset)
        else:
            limit_price = base_price * (1 + self.maker_offset)

        self.pending_order = {
            'direction': direction,
            'limit_price': limit_price,
            'placed_time': timestamp,
            'candles_waited': 0,
            'entry_z': z,
        }
        print(f"⏳ [PENDING] {direction} limit @ {limit_price:.2f} "
              f"(base close={base_price:.2f}, z={z:.2f})")
        self.save_state()

    def _try_fill_pending(self, candle: Dict, timestamp: int, ou_status: Dict) -> bool:
        """Проверяет, коснулась ли цена лимитки. Возвращает True, если исполнено."""
        po = self.pending_order
        direction = po['direction']
        limit_price = po['limit_price']

        filled = False
        if direction == 'BUY' and candle['low'] <= limit_price:
            filled = True
        elif direction == 'SELL' and candle['high'] >= limit_price:
            filled = True

        if not filled:
            return False

        # Используем z на момент исполнения, а не на момент сигнала
        current_z = ou_status.get('z', po.get('entry_z', 0.0))

        self._open_position(direction, limit_price, timestamp,
                            is_maker=True, entry_z=current_z)
        print(f"✅ [FILLED] {direction} @ {limit_price:.2f} (maker, z={current_z:.2f})")
        self.pending_order = None
        self.save_state()
        return True

    def _cancel_pending(self, reason: str = 'timeout'):
        """Отменяет висящий лимитный ордер."""
        if self.pending_order is None:
            return
        po = self.pending_order
        print(f"❌ [CANCEL] {po['direction']} limit @ {po['limit_price']:.2f} ({reason})")
        self.pending_order = None
        self.save_state()

    def _open_position(self, direction: str, price: float, timestamp: int,
                       is_maker: bool = False, entry_z: float = 0.0):
        """Открывает позицию по указанной цене."""
        with self._lock:
            if self.position is not None:
                return

            if is_maker:
                exec_price = price
                entry_fee_rate = self.fee_rate_maker
            else:
                exec_price = price * (1 + self.slippage) if direction == 'BUY' else price * (1 - self.slippage)
                entry_fee_rate = self.fee_rate_taker

            risk_amount = self.balance * self.position_size_pct * self.leverage
            amount = risk_amount / exec_price

            fee = risk_amount * entry_fee_rate
            self.balance -= fee
            self.total_fees += fee

            self.position = {
                'direction': direction,
                'entry_price': exec_price,
                'amount': amount,
                'entry_time': timestamp,
                'entry_fee': fee,
                'entry_fee_rate': entry_fee_rate,
                'is_maker_entry': is_maker,
                'entry_z': entry_z,
                'best_z_abs': abs(entry_z),
                'best_price': exec_price,
                'margin_used': risk_amount / self.leverage,
            }

            self.save_state()

    # ---------- Выход ----------

    def _close_position(self, price: float, timestamp: int, reason: str):
        """Закрывает текущую позицию рыночным ордером (taker)."""
        with self._lock:
            if self.position is None:
                return

            direction = self.position['direction']
            amount = self.position['amount']
            entry_price = self.position['entry_price']

            exec_price = price * (1 - self.slippage) if direction == 'BUY' else price * (1 + self.slippage)
            exit_fee_rate = self.fee_rate_taker

            # Грязный PnL по цене (без комиссий)
            if direction == 'BUY':
                price_pnl = (exec_price - entry_price) * amount
            else:
                price_pnl = (entry_price - exec_price) * amount

            # Комиссии
            entry_fee = self.position.get('entry_fee', 0.0)
            exit_fee = exec_price * amount * exit_fee_rate

            # Чистый PnL — с учётом ОБЕИХ комиссий
            full_pnl = price_pnl - entry_fee - exit_fee

            # Баланс: entry_fee уже списан при открытии,
            # поэтому здесь добавляем только price_pnl − exit_fee.
            # Итог: balance = initial + sum(full_pnl) — сходится с total_pnl.
            self.balance += price_pnl - exit_fee

            self.total_fees += exit_fee
            self.total_pnl += full_pnl

            if full_pnl > 0:
                self.win_count += 1
            else:
                self.loss_count += 1

            hold_minutes = (timestamp - self.position['entry_time']) / 1000.0 / 60.0

            trade = Trade(
                direction=direction,
                entry_price=entry_price,
                exit_price=exec_price,
                entry_time=self.position['entry_time'],
                exit_time=timestamp,
                amount=amount,
                pnl=full_pnl,                          # ← теперь чистый PnL
                fee=entry_fee + exit_fee,              # ← сумма обеих комиссий
                reason=reason,
                is_maker_entry=self.position.get('is_maker_entry', False),
                entry_fee_rate=self.position.get('entry_fee_rate', self.fee_rate_taker),
                exit_fee_rate=exit_fee_rate,
                best_z_abs=self.position.get('best_z_abs'),
                hold_minutes=hold_minutes,
                best_price=self.position.get('best_price'),
            )
            self.trades.append(trade)
            self.position = None

            print(f"🔚 [CLOSE] {direction} {reason} @ {exec_price:.2f} "
                  f"PnL={full_pnl:+.2f} hold={hold_minutes:.1f}min "
                  f"best_z={trade.best_z_abs if trade.best_z_abs is not None else 0.0:.2f} "
                  f"entry={'maker' if trade.is_maker_entry else 'taker'}")

            self.save_state()

    # ---------- Состояние ----------


    def _liquidate(self, timestamp: int, liq_price: float, margin_used: float):
        """
        Принудительное закрытие позиции по цене ликвидации.
        Убыток точно равен margin_used, баланс не уходит в минус.
        """
        with self._lock:
            if self.position is None:
                return

            direction = self.position['direction']
            amount = self.position['amount']
            entry_price = self.position['entry_price']
            entry_fee = self.position.get('entry_fee', 0.0)

            # Закрываемся по liq_price без slippage (это не рыночный ордер, а расчётная цена)
            if direction == 'BUY':
                price_pnl = (liq_price - entry_price) * amount
            else:
                price_pnl = (entry_price - liq_price) * amount

            exit_fee = liq_price * amount * self.fee_rate_taker
            full_pnl = price_pnl - entry_fee - exit_fee

            # Баланс: списываем только price_pnl − exit_fee (entry_fee уже списан при открытии)
            self.balance += price_pnl - exit_fee
            self.total_fees += exit_fee
            self.total_pnl += full_pnl

            # Ликвидация — всегда loss
            self.loss_count += 1

            hold_minutes = (timestamp - self.position['entry_time']) / 1000.0 / 60.0

            trade = Trade(
                direction=direction,
                entry_price=entry_price,
                exit_price=liq_price,
                entry_time=self.position['entry_time'],
                exit_time=timestamp,
                amount=amount,
                pnl=full_pnl,
                fee=entry_fee + exit_fee,
                reason='liquidation',
                is_maker_entry=self.position.get('is_maker_entry', False),
                entry_fee_rate=self.position.get('entry_fee_rate', self.fee_rate_taker),
                exit_fee_rate=self.fee_rate_taker,
                best_z_abs=self.position.get('best_z_abs'),
                hold_minutes=hold_minutes,
                best_price=self.position.get('best_price'),
            )
            self.trades.append(trade)
            self.position = None

            print(f"💥 [LIQUIDATION] {direction} @ {liq_price:.2f} "
                  f"PnL={full_pnl:+.2f} (margin={margin_used:.2f}, "
                  f"loss_pct={full_pnl/margin_used*100:.1f}% of margin)")

            self.save_state()

    def get_status(self) -> Dict:
        """Возвращает текущее состояние демо-трейдера."""
        with self._lock:
            return {
                'balance': self.balance,
                'initial_balance': self.initial_balance,
                'leverage': self.leverage,
                'position': self.position,
                'open_position': self.position is not None,
                'pending_order': self.pending_order,
                'has_pending': self.pending_order is not None,
                'total_pnl': self.total_pnl,
                'total_fees': self.total_fees,
                'win_count': self.win_count,
                'loss_count': self.loss_count,
                'trades_count': len(self.trades),
                'trades': [trade.__dict__ for trade in self.trades],
            }

    def save_state(self) -> None:
        """Сохраняет текущее состояние в JSON-файл."""
        with self._lock:
            state = {
                'balance': self.balance,
                'initial_balance': self.initial_balance,
                'leverage': self.leverage,
                'total_pnl': self.total_pnl,
                'total_fees': self.total_fees,
                'win_count': self.win_count,
                'loss_count': self.loss_count,
                'trades_count': len(self.trades),
                'trades': [trade.__dict__ for trade in self.trades],
                'position': self.position,
                'pending_order': self.pending_order,
            }
            try:
                Path(self.state_file).write_text(
                    json.dumps(state, indent=2, default=str),
                    encoding='utf-8'
                )
            except Exception as e:
                print(f"❌ Ошибка сохранения состояния демо-трейдера: {e}")

    def load_state(self) -> None:
        """Загружает состояние из файла, если он существует."""
        with self._lock:
            path = Path(self.state_file)
            if not path.exists():
                print("ℹ️ Файл состояния демо-трейдера не найден, стартуем с нуля.")
                return

            try:
                state = json.loads(path.read_text(encoding='utf-8'))
                self.balance = state.get('balance', self.initial_balance)
                self.leverage = state.get('leverage', self.leverage)
                self.total_pnl = state.get('total_pnl', 0.0)
                self.total_fees = state.get('total_fees', 0.0)
                self.win_count = state.get('win_count', 0)
                self.loss_count = state.get('loss_count', 0)
                self.position = state.get('position', None)
                self.pending_order = state.get('pending_order', None)

                # Для старых позиций без best_z_abs — восстанавливаем дефолт
                if self.position is not None and 'best_price' not in self.position:
                    self.position['best_price'] = self.position.get('entry_price', 0.0)

                self.trades.clear()
                for trade_data in state.get('trades', []):
                    try:
                        trade = Trade(**trade_data)
                        self.trades.append(trade)
                    except Exception as e:
                        print(f"⚠️ Пропущена некорректная запись сделки: {e}")

                print(f"✅ Состояние демо-трейдера загружено: баланс={self.balance:.2f}, "
                      f"сделок={len(self.trades)}, "
                      f"позиция={'есть' if self.position else 'нет'}, "
                      f"pending={'есть' if self.pending_order else 'нет'}")
            except Exception as e:
                print(f"❌ Ошибка загрузки состояния демо-трейдера: {e}")