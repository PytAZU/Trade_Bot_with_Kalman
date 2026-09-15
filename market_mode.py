"""
Модуль описания торговых режимов (спот / фьючерсы).
Централизует различия между режимами: эндпоинты, категорию REST, state-файлы, плечо.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class MarketMode:
    """Описание торгового режима."""
    key: str                 # "spot" | "futures"
    display_name: str        # "Spot" | "Futures"
    ws_endpoint: str         # WebSocket-эндпоинт Bybit
    rest_category: str       # параметр "category" для REST API
    state_file: str          # путь к JSON-файлу DemoTrader для этого режима
    leverage: float          # 1.0 для спота; для фьючерсов — настраивается


# ---- Определения режимов ----

SPOT_MODE = MarketMode(
    key="spot",
    display_name="Spot",
    ws_endpoint="wss://stream.bybit.com/v5/public/spot",
    rest_category="spot",
    state_file="demo_trader_state_spot.json",
    leverage=1.0,
)

FUTURES_MODE = MarketMode(
    key="futures",
    display_name="Futures",
    ws_endpoint="wss://stream.bybit.com/v5/public/linear",
    rest_category="linear",
    state_file="demo_trader_state_futures.json",
    leverage=1.0,   # при желании можно поднять до 3.0/5.0/10.0
)


# ---- Реестр всех режимов ----

ALL_MODES = {
    SPOT_MODE.key: SPOT_MODE,
    FUTURES_MODE.key: FUTURES_MODE,
}

DEFAULT_MODE_KEY = SPOT_MODE.key


def get_mode(key: str) -> MarketMode:
    """Возвращает объект MarketMode по ключу. Бросает ValueError, если ключ неизвестен."""
    if key not in ALL_MODES:
        raise ValueError(f"Unknown market mode: {key}. Available: {list(ALL_MODES.keys())}")
    return ALL_MODES[key]