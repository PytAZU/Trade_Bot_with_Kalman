"""
Конфигурация приложения Bybit Realtime Chart
Содержит все настройки и константы
"""

import ssl
import certifi
import os
import platform

class Config:
    """Основная конфигурация приложения"""
    
    # Настройки Bybit
    SYMBOL = "BTCUSDT"          # Торговая пара
    TIMEFRAME = 1               # Таймфрейм в минутах
    MAX_CANDLES = 100           # Количество отображаемых свечей
    
    # WebSocket эндпоинты Bybit
    BYBIT_WS_MAIN = "wss://stream.bybit.com/v5/public/spot"
    BYBIT_WS_TESTNET = "wss://stream-testnet.bybit.com/v5/public/spot"
    BYBIT_REST_API = "https://api.bybit.com/v5/market"
    
    # Настройки Flask сервера
    FLASK_HOST = "0.0.0.0"
    FLASK_PORT = 5000
    FLASK_DEBUG = False         # Отключаем debug для корректного завершения
    SECRET_KEY = "bybit_realtime_chart_secret"
    
    # Настройки WebSocket клиента
    WS_PING_INTERVAL = 20       # Интервал ping в секундах
    WS_PING_TIMEOUT = 10        # Таймаут ping
    WS_RECONNECT_DELAY = 5      # Задержка переподключения
    WS_MAX_RETRIES = 5          # Максимальное число попыток
    
    # Настройки завершения
    SHUTDOWN_TIMEOUT = 5        # Таймаут ожидания завершения потоков
    
    # Настройки графика
    CHART_HEIGHT = 700
    CHART_TEMPLATE = "plotly_dark"
    ENABLE_SMA_5 = True         # Показывать SMA 5
    ENABLE_SMA_10 = True        # Показывать SMA 10

    # Параметры фильтра Калмана
    KALMAN_DELTA_T = 1.0         # интервал времени (соответствует 1 свече)
    KALMAN_Q_LEVEL = 0.01        # шум процесса для уровня
    KALMAN_Q_TREND = 0.001       # шум процесса для тренда
    KALMAN_R = 10000.0           # шум измерения (примерно (100$)^2)
    
    # Цветовая схема
    COLORS = {
        "bullish": "#00ff88",    # Цвет бычьих свечей (зеленый)
        "bearish": "#ff4444",    # Цвет медвежьих свечей (красный)
        "sma_5": "#ffa500",      # Цвет SMA 5 (оранжевый)
        "sma_10": "#00bfff",     # Цвет SMA 10 (голубой)
        "background": "#1a1a2e",
        "surface": "#16213e",
        "text": "#ffffff",
        "grid": "rgba(255, 255, 255, 0.1)"
    }

    @staticmethod
    def create_ssl_context():
        """Создание SSL контекста с обработкой ошибок"""
        print("\n🔒 НАСТРОЙКА SSL КОНТЕКСТА")
        print(f"  ОС: {platform.system()} {platform.release()}")
        print(f"  Python: {platform.python_version()}")
        print(f"  certifi версия: {certifi.__version__}")
        print(f"  Путь к сертификатам certifi: {certifi.where()}")
        
        if os.path.exists(certifi.where()):
            print(f"  ✅ Файл сертификатов certifi существует")
        else:
            print(f"  ❌ Файл сертификатов certifi НЕ НАЙДЕН!")
        
        # Способ 1: Использовать сертификаты certifi
        try:
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            print("  ✅ Способ 1: SSL контекст создан с использованием certifi")
            return ssl_context
        except Exception as e:
            print(f"  ⚠️ Способ 1 не сработал: {e}")
        
        # Способ 2: Использовать системные сертификаты
        try:
            ssl_context = ssl.create_default_context()
            ssl_context.load_default_certs()
            print("  ✅ Способ 2: SSL контекст создан с системными сертификатами")
            return ssl_context
        except Exception as e:
            print(f"  ⚠️ Способ 2 не сработал: {e}")
        
        # Способ 3: Незащищенный контекст
        print("\n  ⚠️ ВНИМАНИЕ! Используется НЕЗАЩИЩЕННЫЙ SSL контекст.")
        print("  ⚠️ Это приемлемо только для разработки/тестирования!")
        
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        
        print("  ✅ Способ 3: Создан незащищенный SSL контекст (для разработки)")
        return ssl_context
    
    @staticmethod
    def format_timeframe(timeframe_minutes):
        """Преобразование таймфрейма в формат Bybit"""
        if timeframe_minutes >= 60:
            return f"{timeframe_minutes // 60}H"
        elif timeframe_minutes >= 1:
            return f"{timeframe_minutes}"
        else:
            return "1"