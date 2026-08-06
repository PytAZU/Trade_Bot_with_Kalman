"""
Модуль фильтра Калмана для оценки "справедливой цены".
Использует модель локального линейного тренда (уровень + скорость).
"""

import numpy as np

class KalmanFilter:
    """
    Одномерный фильтр Калмана для отслеживания скрытого состояния (уровень и тренд).
    Параметры:
        delta_t : float        – интервал времени между наблюдениями (обычно 1)
        q_level : float        – дисперсия шума процесса для уровня
        q_trend : float        – дисперсия шума процесса для тренда
        r       : float        – дисперсия шума измерения
    """

    def __init__(self, delta_t=1.0, q_level=0.01, q_trend=0.001, r=10000.0):
        self.delta_t = delta_t

        # Матрицы модели
        self.F = np.array([[1.0, delta_t],
                           [0.0, 1.0]])          # переходная матрица
        self.H = np.array([[1.0, 0.0]])          # матрица наблюдения

        # Ковариационные матрицы шумов
        self.Q = np.diag([q_level, q_trend])     # процесса
        self.R = np.array([[r]])                  # измерения

        # Начальное состояние и ковариация ошибки
        self.x = np.array([[0.0], [0.0]])        # [уровень, тренд]
        self.P = np.eye(2) * 1000.0              # большое начальное значение
        self.initialized = False

    def update(self, measurement: float) -> float:
        """
        Принимает новое измерение (цену), обновляет фильтр и возвращает
        текущую оценку уровня (справедливую цену).
        """
        if not self.initialized:
            # Инициализация первым значением
            self.x[0, 0] = measurement
            self.initialized = True
            return measurement

        # --- Прогноз (prediction) ---
        x_pred = self.F @ self.x
        P_pred = self.F @ self.P @ self.F.T + self.Q

        # --- Коррекция (update) ---
        y = measurement - (self.H @ x_pred)[0, 0]      # невязка
        S = (self.H @ P_pred @ self.H.T + self.R)[0, 0]  # ковариация невязки
        K = (P_pred @ self.H.T) / S                     # коэффициент Калмана (2x1)

        self.x = x_pred + K * y
        self.P = (np.eye(2) - K @ self.H) @ P_pred

        return float(self.x[0, 0])