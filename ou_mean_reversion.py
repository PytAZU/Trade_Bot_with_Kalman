"""
Модуль стратегии возврата к среднему на основе процесса Орнштейна-Уленбека (OU).

Использует спред между рыночной ценой и оценкой фильтра Калмана.
Вычисляет z-score отклонения и генерирует сигналы входа/выхода.
"""

from collections import deque
import numpy as np


class OUMeanReversion:
    """
    Реализация процесса Орнштейна-Уленбека для торговой стратегии.

    Параметры:
        window          : int   – максимальная длина буфера спреда
        min_obs         : int   – минимальное число наблюдений для оценки
        entry_z         : float – порог z-score для входа (абсолютное значение)
        exit_z          : float – порог z-score для выхода
        stop_z          : float – порог z-score для принудительного стоп-лосса
        delta_t         : float – интервал времени между свечами (по умолчанию 1)
    """

    def __init__(
        self,
        window: int = 200,
        min_obs: int = 50,
        entry_z: float = 2.0,
        exit_z: float = 0.5,
        stop_z: float = 3.5,
        delta_t: float = 1.0,
    ):
        self.window = window
        self.min_obs = min_obs
        self.entry_z = entry_z
        self.exit_z = exit_z
        self.stop_z = stop_z
        self.delta_t = delta_t

        # Буфер спреда (X_t)
        self.spread_buffer = deque(maxlen=window)

        # Оценки параметров OU
        self.theta = None
        self.mu = None
        self.sigma = None

        # Текущее значение z-score и сигнал
        self.current_z = None
        self.current_signal = "FLAT"  # возможные значения: BUY, SELL, FLAT, STOP

        # Флаг, показывающий, что модель готова (достаточно данных)
        self.ready = False

    def update(self, spread: float) -> dict:
        """
        Добавляет новое значение спреда и пересчитывает параметры модели.

        Args:
            spread: Текущее отклонение цены от справедливой (close - kalman_fair_price)

        Returns:
            dict с ключами:
                - 'spread'      : float – переданный спред
                - 'z'           : float или None – текущий z-score
                - 'theta'       : float или None – скорость возврата
                - 'sigma'       : float или None – волатильность спреда
                - 'mu'          : float или None – долгосрочное среднее
                - 'signal'      : str   – торговый сигнал (BUY/SELL/FLAT/STOP)
                - 'ready'       : bool  – готова ли модель
        """
        self.spread_buffer.append(spread)

        # Пока недостаточно данных, z-score не считаем
        if len(self.spread_buffer) < self.min_obs:
            self.ready = False
            self.current_z = None
            self.current_signal = "FLAT"
            return self._result(spread)

        # Оценка параметров AR(1): X_t = c + phi * X_{t-1} + eps
        # Используем обычный МНК.
        x = np.array(self.spread_buffer)
        x_lag = x[:-1]
        x_curr = x[1:]

        # Добавляем константу (для оценки c)
        X_design = np.column_stack([np.ones(len(x_lag)), x_lag])
        try:
            # beta = [c, phi]
            beta, _, _, _ = np.linalg.lstsq(X_design, x_curr, rcond=None)
        except np.linalg.LinAlgError:
            self.ready = False
            self.current_z = None
            self.current_signal = "FLAT"
            return self._result(spread)

        c = beta[0]
        phi = beta[1]

        # Защита от нестационарности: phi должно быть < 1 (иначе процесс не возвращается)
        if phi >= 1.0:
            # Можно ограничить phi, например, 0.999
            phi = 0.999
        if phi <= -1.0:
            phi = -0.999

        # Параметры OU
        self.theta = (1 - phi) / self.delta_t
        self.mu = c / (1 - phi) if phi != 1 else 0.0
        # Стандартное отклонение остатков
        residuals = x_curr - (c + phi * x_lag)
        sigma_eta = float(np.std(residuals, ddof=1)) if len(residuals) > 1 else 0.0
        self.sigma = sigma_eta / np.sqrt(self.delta_t) if self.delta_t > 0 else sigma_eta

        # Стационарная дисперсия процесса OU: Var = sigma^2 / (2*theta)
        if self.theta > 0 and self.sigma > 0:
            stat_var = self.sigma ** 2 / (2 * self.theta)
            stat_std = np.sqrt(stat_var)
        else:
            stat_std = 0.0

        # Текущий z-score
        if stat_std > 0:
            self.current_z = (spread - self.mu) / stat_std
        else:
            self.current_z = 0.0

        self.ready = True

        # Генерация сигнала
        self.current_signal = self._evaluate_signal(self.current_z)

        return self._result(spread)

    def _evaluate_signal(self, z: float) -> str:
        """Определяет торговый сигнал на основе z-score."""
        abs_z = abs(z)

        if abs_z >= self.stop_z:
            return "STOP"
        if z >= self.entry_z:
            return "SELL"   # спред слишком высок, продаём (ожидаем снижения)
        if z <= -self.entry_z:
            return "BUY"    # спред слишком низок, покупаем (ожидаем роста)
        if abs_z <= self.exit_z:
            return "FLAT"   # возврат к среднему, закрываем позицию
        # Между exit и entry – удерживаем текущую позицию (нейтрально)
        return "FLAT"

    def _result(self, spread: float) -> dict:
        """Формирует словарь результата."""
        return {
            'spread': float(spread),
            'z': self.current_z,
            'theta': self.theta,
            'sigma': self.sigma,
            'mu': self.mu,
            'signal': self.current_signal,
            'ready': self.ready,
        }

    def get_status(self) -> dict:
        """Возвращает текущее состояние модели."""
        return {
            'ready': self.ready,
            'z': self.current_z,
            'theta': self.theta,
            'sigma': self.sigma,
            'mu': self.mu,
            'signal': self.current_signal,
            'buffer_len': len(self.spread_buffer),
        }