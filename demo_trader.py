"""
Демо-торговый модуль для тестирования стратегии возврата к среднему.
Позволяет эмулировать исполнение сделок на исторических/реальных данных,
используя сигналы от процесса Орнштейна-Уленбека (OU).

Баланс виртуальный, сделки не отправляются на биржу.
"""

import json
from pathlib import Path
from typing import Optional, List, Dict, Union

from dataclasses import dataclass, field
from typing import Optional, List, Dict


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
    reason: str             # причина закрытия ('exit', 'stop', 'reverse')


class DemoTrader:
    """
    Эмулятор торговли с использованием сигналов OU.
    Позиции открываются при |z| > entry_z и закрываются при |z| < exit_z или стопе.

    Все параметры задаются в этом классе (можно позже перенести в config).
    """

    def __init__(self, initial_balance: float = 10000.0,
                 position_size_pct: float = 0.1,      # доля баланса на одну позицию
                 fee_rate: float = 0.0006,            # комиссия (0.06% за сделку)
                 slippage: float = 0.0001,            # проскальзывание (0.01%)
                 entry_z: float = 2.0,
                 exit_z: float = 0.5,
                 stop_z: float = 3.5,
                 state_file: str = "demo_trader_state.json"):
        """
        Args:
            initial_balance: начальный баланс в USDT
            position_size_pct: доля баланса, используемая для расчёта объёма позиции
            fee_rate: комиссия за каждую сторону (вход/выход)
            slippage: проскальзывание при исполнении (добавляется к цене)
            entry_z: порог z-score для входа (абсолютное значение)
            exit_z: порог z-score для выхода
            stop_z: порог z-score для стоп-лосса
        """
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.position_size_pct = position_size_pct
        self.fee_rate = fee_rate
        self.slippage = slippage
        self.entry_z = entry_z
        self.exit_z = exit_z
        self.stop_z = stop_z

        self.state_file = state_file # сохранил путь
        self.load_state()  # загрузка сохраненного состояния

        # Текущая открытая позиция
        self.position: Optional[Dict] = None   # {direction, entry_price, amount, entry_time}

        # Список завершённых сделок
        self.trades: List[Trade] = []

        # Для статистики
        self.total_pnl = 0.0
        self.total_fees = 0.0
        self.win_count = 0
        self.loss_count = 0

    def reset(self):
        """Сброс всех параметров к начальным."""
        self.balance = self.initial_balance
        self.position = None
        self.trades.clear()
        self.total_pnl = 0.0
        self.total_fees = 0.0
        self.win_count = 0
        self.loss_count = 0

    def update(self, price: float, ou_status: Dict, timestamp: int) -> None:
        """
        Вызывается на каждой закрытой свече с текущей ценой и статусом OU.

        Args:
            price: цена закрытия свечи
            ou_status: словарь из OUMeanReversion.update() (содержит z, signal, theta, sigma)
            timestamp: время закрытия свечи (мс)
        """
        if not ou_status.get('ready', False):
            return  # Модель ещё не готова, не торгуем

        z = ou_status.get('z', 0.0)
        signal = ou_status.get('signal', 'FLAT')

        # Проверяем стоп-лосс, если позиция открыта
        if self.position is not None:
            if abs(z) >= self.stop_z:
                self._close_position(price, timestamp, reason='stop')
                return

            # Закрытие по возврату к среднему
            if abs(z) <= self.exit_z:
                self._close_position(price, timestamp, reason='exit')
                return

            # Реверс: если сигнал противоположный и z превысил entry_z
            if self.position['direction'] == 'BUY' and z <= -self.entry_z:
                self._close_position(price, timestamp, reason='reverse')
                self._open_position('SELL', price, timestamp)
            elif self.position['direction'] == 'SELL' and z >= self.entry_z:
                self._close_position(price, timestamp, reason='reverse')
                self._open_position('BUY', price, timestamp)
        else:
            # Позиции нет, открываем при сильном отклонении
            if z <= -self.entry_z and signal == 'BUY':
                self._open_position('BUY', price, timestamp)
            elif z >= self.entry_z and signal == 'SELL':
                self._open_position('SELL', price, timestamp)

    def _open_position(self, direction: str, price: float, timestamp: int):
        """Открывает позицию указанного направления."""
        if self.position is not None:
            return  # Уже есть позиция

        # Применяем проскальзывание
        exec_price = price * (1 + self.slippage) if direction == 'BUY' else price * (1 - self.slippage)

        # Рассчитываем объём: доля баланса / цена
        risk_amount = self.balance * self.position_size_pct
        amount = risk_amount / exec_price

        # Комиссия за вход
        fee = risk_amount * self.fee_rate
        self.balance -= fee
        self.total_fees += fee

        self.position = {
            'direction': direction,
            'entry_price': exec_price,
            'amount': amount,
            'entry_time': timestamp,
        }

    def _close_position(self, price: float, timestamp: int, reason: str):
        """Закрывает текущую позицию и фиксирует сделку."""
        if self.position is None:
            return

        direction = self.position['direction']
        amount = self.position['amount']
        entry_price = self.position['entry_price']

        # Применяем проскальзывание в обратную сторону
        exec_price = price * (1 - self.slippage) if direction == 'BUY' else price * (1 + self.slippage)

        # PnL
        if direction == 'BUY':
            pnl = (exec_price - entry_price) * amount
        else:
            pnl = (entry_price - exec_price) * amount

        # Комиссия за выход
        exit_fee = exec_price * amount * self.fee_rate
        pnl -= exit_fee
        self.balance += pnl
        self.total_fees += exit_fee
        self.total_pnl += pnl

        if pnl > 0:
            self.win_count += 1
        else:
            self.loss_count += 1

        trade = Trade(
            direction=direction,
            entry_price=entry_price,
            exit_price=exec_price,
            entry_time=self.position['entry_time'],
            exit_time=timestamp,
            amount=amount,
            pnl=pnl,
            fee=exit_fee + self.position.get('entry_fee', 0),
            reason=reason
        )
        self.trades.append(trade)
        self.position = None

        self.save_state()

    def get_status(self) -> Dict:
        """Возвращает текущее состояние демо-трейдера."""
        return {
            'balance': self.balance,
            'initial_balance': self.initial_balance,
            'position': self.position,
            'open_position': self.position is not None,
            'total_pnl': self.total_pnl,
            'total_fees': self.total_fees,
            'win_count': self.win_count,
            'loss_count': self.loss_count,
            'trades_count': len(self.trades),
            'trades': [trade.__dict__ for trade in self.trades]
        }

    def save_state(self) -> None:
        """Сохраняет текущее состояние в JSON-файл."""
        state = {
            'balance': self.balance,
            'initial_balance': self.initial_balance,
            'total_pnl': self.total_pnl,
            'total_fees': self.total_fees,
            'win_count': self.win_count,
            'loss_count': self.loss_count,
            'trades_count': len(self.trades),
            'trades': [trade.__dict__ for trade in self.trades],
            'position': self.position,
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
        path = Path(self.state_file)
        if not path.exists():
            print("ℹ️ Файл состояния демо-трейдера не найден, стартуем с нуля.")
            return

        try:
            state = json.loads(path.read_text(encoding='utf-8'))
            self.balance = state.get('balance', self.initial_balance)
            self.total_pnl = state.get('total_pnl', 0.0)
            self.total_fees = state.get('total_fees', 0.0)
            self.win_count = state.get('win_count', 0)
            self.loss_count = state.get('loss_count', 0)
            self.position = state.get('position', None)

            # Восстанавливаем сделки
            self.trades.clear()
            for trade_data in state.get('trades', []):
                try:
                    trade = Trade(**trade_data)
                    self.trades.append(trade)
                except Exception as e:
                    print(f"⚠️ Пропущена некорректная запись сделки: {e}")
            print(f"✅ Состояние демо-трейдера загружено: баланс={self.balance:.2f}, сделок={len(self.trades)}")
        except Exception as e:
            print(f"❌ Ошибка загрузки состояния демо-трейдера: {e}")