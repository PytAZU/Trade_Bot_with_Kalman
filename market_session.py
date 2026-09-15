"""
Контейнер состояния одного торгового режима (spot / futures).

Каждый MarketMode получает собственный экземпляр MarketSession,
в котором изолированно живут:
  - свечи и оценки индикаторов (свечи, Kalman, OU, Z-история);
  - текущая формирующаяся свеча и метка времени последней;
  - сигнал OU;
  - DemoTrader со своим state-файлом и своим балансом.

Блокировкой (RLock) владеет BybitDataCollector — она общая для всех
сессий и обеспечивает атомарность операций при переключении режима.
"""

from collections import deque

from kalman_filter import KalmanFilter
from ou_mean_reversion import OUMeanReversion
from demo_trader import DemoTrader
from market_mode import MarketMode


class MarketSession:
    """Изолированное состояние одного торгового режима."""

    def __init__(self, mode: MarketMode, config):
        """
        Args:
            mode:   объект MarketMode (spot или futures) — источник
                    endpoint-ов, category, state-файла и плеча.
            config: объект Config — общие параметры индикаторов и DemoTrader.
        """
        self.mode = mode
        self.config = config
        self.leverage = mode.leverage  # задел на будущий ползунок плеча

        # ---- Свечи и оценки индикаторов ----
        self.candles_data = deque(maxlen=config.MAX_CANDLES)
        self.kalman_estimates = deque(maxlen=config.MAX_CANDLES)
        self.ou_z_history = deque(maxlen=config.MAX_CANDLES)

        # ---- Текущая свеча ----
        self.current_candle = None
        self.last_candle_time = None

        # ---- Фильтр Калмана ----
        self.kalman = KalmanFilter(
            delta_t=config.KALMAN_DELTA_T,
            q_level=config.KALMAN_Q_LEVEL,
            q_trend=config.KALMAN_Q_TREND,
            r=config.KALMAN_R,
        )

        # ---- Процесс Орнштейна-Уленбека ----
        self.ou = OUMeanReversion(
            window=config.OU_WINDOW,
            min_obs=config.OU_MIN_OBS,
            entry_z=config.OU_ENTRY_Z,
            exit_z=config.OU_EXIT_Z,
            stop_z=config.OU_STOP_Z,
            delta_t=config.OU_DELTA_T,
        )
        self.ou_signal = None

        # ---- Демо-трейдер ----
        # У спота и фьючерсов — разные state-файлы, поэтому их истории сделок
        # и балансы полностью независимы.
        self.demo_trader = DemoTrader(
            initial_balance=config.DEMO_INITIAL_BALANCE,
            position_size_pct=config.DEMO_POSITION_SIZE_PCT,
            fee_rate=config.DEMO_FEE_RATE,
            slippage=config.DEMO_SLIPPAGE,
            entry_z=config.DEMO_ENTRY_Z,
            exit_z=config.DEMO_EXIT_Z,
            stop_z=config.DEMO_STOP_Z,
            leverage=mode.leverage,
            state_file=mode.state_file,
        )

    def reset_history(self):
        """
        Сброс накопленной истории индикаторов (свечи, Kalman, OU, Z-score).
        DemoTrader НЕ сбрасывается — его баланс и история сделок сохраняются.
        """
        self.candles_data.clear()
        self.kalman_estimates.clear()
        self.ou_z_history.clear()
        self.current_candle = None
        self.last_candle_time = None
        self.ou_signal = None

        # Пересоздаём индикаторы — их внутреннее состояние относилось к прежнему ряду
        self.kalman = KalmanFilter(
            delta_t=self.config.KALMAN_DELTA_T,
            q_level=self.config.KALMAN_Q_LEVEL,
            q_trend=self.config.KALMAN_Q_TREND,
            r=self.config.KALMAN_R,
        )
        self.ou = OUMeanReversion(
            window=self.config.OU_WINDOW,
            min_obs=self.config.OU_MIN_OBS,
            entry_z=self.config.OU_ENTRY_Z,
            exit_z=self.config.OU_EXIT_Z,
            stop_z=self.config.OU_STOP_Z,
            delta_t=self.config.OU_DELTA_T,
        )