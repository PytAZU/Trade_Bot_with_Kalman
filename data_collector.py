"""
Модуль сбора данных с Bybit
Отвечает за подключение к WebSocket и получение свечей в реальном времени.

Поддерживает несколько торговых режимов (spot / futures) через независимые
MarketSession. Активный режим определяется self.active_mode, а состояние
каждого режима изолировано в отдельной сессии.
"""

import asyncio
import json
import time
import ssl
from datetime import datetime
from threading import RLock
import websockets
import requests
import urllib3

from market_mode import ALL_MODES, DEFAULT_MODE_KEY
from market_session import MarketSession

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class BybitDataCollector:
    """Сборщик данных с биржи Bybit"""

    def __init__(self, config):
        self.config = config
        self.symbol = config.SYMBOL
        self.timeframe = config.TIMEFRAME
        self.max_candles = config.MAX_CANDLES
        self.interval = config.format_timeframe(config.TIMEFRAME)

        # ---- Статистика 24ч (для активного режима) ----
        self.high_24h = 0
        self.low_24h = float('inf')
        self.volume_24h = 0
        self.last_price = 0

        # ---- Состояние процесса ----
        self.is_running = True
        self.is_connected = False
        self.ssl_context = config.create_ssl_context()

        # ---- WebSocket ----
        self.websocket = None
        self.ws_loop = None  # event loop WS-потока (для закрытия сокета из другого потока)

        # ---- Блокировка состояния (общая для всех сессий) ----
        self._data_lock = RLock()

        # ---- Флаг запроса на переключение режима (используется в шаге 5) ----
        self._reconnect_requested = False

        # ---- Сессии для каждого режима ----
        self.sessions = {
            key: MarketSession(mode, config)
            for key, mode in ALL_MODES.items()
        }

        # ---- Активный режим ----
        default_key = getattr(config, "DEFAULT_MARKET_MODE", DEFAULT_MODE_KEY)
        if default_key not in self.sessions:
            print(f"⚠️ Неизвестный режим '{default_key}', используется '{DEFAULT_MODE_KEY}'")
            default_key = DEFAULT_MODE_KEY
        self.active_mode = ALL_MODES[default_key]

        # ---- Загрузка начальных данных для активного режима ----
        self.fetch_initial_candles()
        self.fetch_24h_stats()


    

    # ---------- Удобный доступ к активной сессии ----------

    @property
    def session(self) -> MarketSession:
        """Возвращает активную MarketSession."""
        return self.sessions[self.active_mode.key]

    # ---------- Загрузка исторических данных ----------

    def fetch_initial_candles(self):
        """Получение начальных исторических данных через REST API"""
        session = self.session
        try:
            url = f"{self.config.BYBIT_REST_API}/kline"
            params = {
                "category": self.active_mode.rest_category,
                "symbol": self.symbol,
                "interval": self.interval,
                "limit": self.max_candles,
            }

            print(f"\n{'='*60}")
            print(f"📡 ЗАПРОС ИСТОРИЧЕСКИХ ДАННЫХ [{self.active_mode.display_name}]")
            print(f"{'='*60}")
            print(f"URL: {url}")
            print(f"Пара: {self.symbol}")
            print(f"Таймфрейм: {self.interval}min")
            print(f"Режим: {self.active_mode.key}")
            print(f"Количество свечей: {self.max_candles}")

            try:
                response = requests.get(url, params=params, verify=True, timeout=10)
            except requests.exceptions.SSLError:
                print("⚠️ Ошибка SSL, пробую без проверки сертификата...")
                response = requests.get(url, params=params, verify=False, timeout=10)

            data = response.json()

            if data.get("retCode") == 0:
                candles = data["result"]["list"]

                if candles and isinstance(candles, list):
                    candles.sort(key=lambda x: int(x[0]) if isinstance(x, list)
                                 else int(x.get('start', x.get('timestamp', 0))))

                    parsed_candles = []
                    for candle in candles:
                        if isinstance(candle, list):
                            timestamp = int(candle[0])
                            open_price = float(candle[1])
                            high_price = float(candle[2])
                            low_price = float(candle[3])
                            close_price = float(candle[4])
                            volume = float(candle[5]) if len(candle) > 5 else 0.0
                        elif isinstance(candle, dict):
                            timestamp = int(candle.get('start', candle.get('timestamp', 0)))
                            open_price = float(candle.get('open', 0))
                            high_price = float(candle.get('high', 0))
                            low_price = float(candle.get('low', 0))
                            close_price = float(candle.get('close', 0))
                            volume = float(candle.get('volume', 0))
                        else:
                            continue

                        parsed_candles.append({
                            'timestamp': timestamp,
                            'open': open_price,
                            'high': high_price,
                            'low': low_price,
                            'close': close_price,
                            'volume': volume,
                        })

                    print(f"\n🕯️ ЗАГРУЖЕННЫЕ СВЕЧИ:")
                    for i, candle in enumerate(parsed_candles, 1):
                        time_str = datetime.fromtimestamp(candle['timestamp']/1000).strftime('%Y-%m-%d %H:%M:%S')
                        change = candle['close'] - candle['open']
                        change_percent = (change / candle['open']) * 100 if candle['open'] > 0 else 0

                        if i <= 5 or i > len(parsed_candles) - 5:
                            print(f"  {i:3d}. [{time_str}] O:{candle['open']:12.2f} H:{candle['high']:12.2f} "
                                  f"L:{candle['low']:12.2f} C:{candle['close']:12.2f} Vol:{candle['volume']:10.4f} "
                                  f"({change:+.2f} / {change_percent:+.2f}%)")
                        elif i == 6:
                            print(f"  ... (пропущено {len(parsed_candles) - 10} свечей) ...")

                    # Последняя свеча из REST — текущая незакрытая минута; в историю не кладём
                    closed_candles = parsed_candles[:-1] if parsed_candles else []
                    for candle in closed_candles:
                        session.candles_data.append(candle)

                    if parsed_candles:
                        last_candle = parsed_candles[-1]
                        session.current_candle = {**last_candle, 'confirm': False}
                        session.last_candle_time = last_candle['timestamp']

                    print(f"\n✅ ЗАГРУЖЕНО {len(session.candles_data)} ЗАКРЫТЫХ СВЕЧЕЙ")
                    if session.current_candle:
                        print(f"⏳ Текущая формирующаяся свеча вынесена отдельно")
            else:
                print(f"❌ ОШИБКА API: {data.get('retMsg', 'Unknown error')}")

            # Инициализация фильтра Калмана и процесса OU историческими данными
            if len(session.candles_data) > 0:
                for candle in session.candles_data:
                    fair_price = session.kalman.update(candle['close'])
                    session.kalman_estimates.append(fair_price)

                    spread = candle['close'] - fair_price
                    ou_result = session.ou.update(spread)
                    session.ou_z_history.append(ou_result.get('z'))

                print(f"📈 Фильтр Калмана инициализирован {len(session.kalman_estimates)} оценками")
                print(f"📊 Процесс OU обновлён на {len(session.candles_data)} исторических свечах")
                print(f"📉 История Z-score заполнена: {len(session.ou_z_history)} значений")

        except Exception as e:
            print(f"❌ ОШИБКА при получении начальных данных: {e}")
            import traceback
            traceback.print_exc()

    def fetch_24h_stats(self):
        """Получение 24-часовой статистики для активного режима"""
        try:
            url = f"{self.config.BYBIT_REST_API}/tickers"
            params = {
                "category": self.active_mode.rest_category,
                "symbol": self.symbol,
            }

            try:
                response = requests.get(url, params=params, verify=True, timeout=10)
            except requests.exceptions.SSLError:
                response = requests.get(url, params=params, verify=False, timeout=10)

            data = response.json()

            if data.get("retCode") == 0 and data.get("result", {}).get("list"):
                ticker = data["result"]["list"][0]
                self.high_24h = float(ticker.get("highPrice24h", 0))
                self.low_24h = float(ticker.get("lowPrice24h", 0))
                self.volume_24h = float(ticker.get("volume24h", 0))
                self.last_price = float(ticker.get("lastPrice", 0))

                print(f"📊 24Ч СТАТИСТИКА [{self.active_mode.display_name}]:")
                print(f"  Максимум: ${self.high_24h:,.2f}")
                print(f"  Минимум:  ${self.low_24h:,.2f}")
                print(f"  Объем:    {self.volume_24h:,.2f}")
                print(f"  Последняя цена: ${self.last_price:,.2f}")

        except Exception as e:
            print(f"❌ Ошибка получения 24ч статистики: {e}")

    def switch_market(self, mode_key: str):
        """
        Переключение активного торгового режима.

        Порядок:
        1. Сохранить DemoTrader текущей сессии.
        2. Установить _reconnect_requested — блокирует приём данных от старого WS.
        3. Сменить active_mode и сбросить историю индикаторов.
        4. Принудительно закрыть текущее WS-соединение (recv() упадёт → цикл переподключится).
        5. Загрузить историю и статистику нового режима через REST.
        """
        if mode_key not in self.sessions:
            raise ValueError(f"Unknown market mode: {mode_key}")

        if mode_key == self.active_mode.key:
            print(f"ℹ️ Режим '{mode_key}' уже активен")
            return

        print(f"\n{'='*60}")
        print(f"🔄 ПЕРЕКЛЮЧЕНИЕ РЕЖИМА: {self.active_mode.key} → {mode_key}")
        print(f"{'='*60}")

        with self._data_lock:
            # 1. Сохраняем DemoTrader текущей сессии
            self.session.demo_trader.save_state()

            # 2. Флаг: блокируем приём данных и просим WS-цикл переподключиться
            self._reconnect_requested = True

            # 3. Меняем активный режим и сбрасываем историю целевой сессии
            self.active_mode = ALL_MODES[mode_key]
            self.session.reset_history()

        # 4. Принудительно закрываем текущий WS — recv() падает с ConnectionClosed,
        #    внешний while переходит на новую итерацию с новым uri.
        self._force_close_websocket()

        # 5. Загружаем историю и статистику нового режима через REST
        self.fetch_initial_candles()
        self.fetch_24h_stats()

        print(f"✅ Режим переключён на '{mode_key}'")

    def set_leverage(self, leverage: float):
        """Устанавливает кредитное плечо для активной сессии."""
        with self._data_lock:
            self.session.demo_trader.set_leverage(leverage)
            print(f"⚙️ Плечо [{self.active_mode.display_name}] установлено: x{self.session.demo_trader.leverage:.1f}")

    def _force_close_websocket(self):
        """Закрывает активный WebSocket из другого потока (HTTP-потока)."""
        if self.websocket is None or self.ws_loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.websocket.close(),
                self.ws_loop
            )
        except Exception as e:
            print(f"⚠️ Не удалось закрыть WebSocket: {e}")

    # ---------- WebSocket ----------

    async def connect_websocket(self):
        """Подключение к WebSocket Bybit и получение данных"""
        print(f"\n{'='*60}")
        print(f"📡 ПОДКЛЮЧЕНИЕ К WEBSOCKET")
        print(f"{'='*60}")

        # Сохраняем ссылку на event loop — понадобится для закрытия сокета
        # из другого потока (HTTP-потока) при переключении режима.
        self.ws_loop = asyncio.get_running_loop()

        while self.is_running:
            uri = self.active_mode.ws_endpoint
            print(f"URI: {uri} (режим: {self.active_mode.key})")
            reconnect_immediately = False

            try:
                async with websockets.connect(
                    uri,
                    ssl=self.ssl_context,
                    ping_interval=self.config.WS_PING_INTERVAL,
                    ping_timeout=self.config.WS_PING_TIMEOUT,
                    close_timeout=5,
                ) as websocket:

                    self.websocket = websocket
                    self.is_connected = True

                    # Переподключение успешно — снимаем флаг, возобновляем приём данных
                    self._reconnect_requested = False

                    print(f"✅ WEBSOCKET ПОДКЛЮЧЕН [{self.active_mode.display_name}]")

                    # Подписка на свечи
                    subscribe_msg = {
                        "op": "subscribe",
                        "args": [f"kline.{self.interval}.{self.symbol}"],
                    }
                    await websocket.send(json.dumps(subscribe_msg))

                    print(f"📡 Подписка на kline.{self.interval}.{self.symbol}")
                    print(f"  Ожидание данных...\n")

                    while self.is_running:
                        # Проверка запроса на смену режима (задел на шаг 5)
                        if self._reconnect_requested:
                            print("🔄 Запрошено переключение режима, переподключаемся...")
                            self._reconnect_requested = False
                            reconnect_immediately = True
                            break

                        try:
                            response = await asyncio.wait_for(websocket.recv(), timeout=30)
                            data = json.loads(response)

                            if "topic" in data and "kline" in data["topic"]:
                                self.process_kline_data(data)
                            elif "success" in data:
                                print(f"✅ ПОДПИСКА ПОДТВЕРЖДЕНА\n")

                        except asyncio.TimeoutError:
                            if not self.is_running:
                                break
                            try:
                                await websocket.ping()
                            except:
                                break
                            continue
                        except websockets.exceptions.ConnectionClosed:
                            print("⚠️ WebSocket соединение закрыто сервером")
                            break

                    # Нормальное завершение
                    if not self.is_running:
                        print("🛑 Получен сигнал остановки WebSocket клиента")
                        await self._unsubscribe(websocket)
                        break

            except ssl.SSLError as e:
                print(f"❌ SSL ОШИБКА: {e}")
                print("⚠️ Переключение на незащищенный SSL...")
                self.ssl_context.check_hostname = False
                self.ssl_context.verify_mode = ssl.CERT_NONE

            except Exception as e:
                if not self.is_running:
                    print("🛑 WebSocket клиент остановлен")
                    break
                print(f"❌ ОШИБКА WEBSOCKET: {type(e).__name__}: {e}")

            finally:
                self.is_connected = False
                self.websocket = None

            if not self.is_running:
                break

            # При переключении режима переподключаемся сразу, без задержки
            if not reconnect_immediately:
                print(f"\n🔄 ПЕРЕПОДКЛЮЧЕНИЕ через {self.config.WS_RECONNECT_DELAY} секунд...")
                await asyncio.sleep(self.config.WS_RECONNECT_DELAY)

        print("✅ WebSocket клиент завершил работу")

    async def _unsubscribe(self, websocket):
        """Отписка от каналов при завершении"""
        try:
            unsubscribe_msg = {
                "op": "unsubscribe",
                "args": [f"kline.{self.interval}.{self.symbol}"],
            }
            await websocket.send(json.dumps(unsubscribe_msg))
            print("📡 Отправлена отписка от каналов")
            await asyncio.sleep(0.5)
        except:
            pass

    def shutdown(self):
        """Корректное завершение работы сборщика данных"""
        print("\n🛑 ЗАВЕРШЕНИЕ РАБОТЫ СБОРЩИКА ДАННЫХ")
        print("  Ожидание завершения WebSocket соединения...")
        self.is_running = False

        # Сохраняем состояние ВСЕХ сессий (и спота, и фьючерсов)
        for key, session in self.sessions.items():
            try:
                session.demo_trader.save_state()
            except Exception as e:
                print(f"⚠️ Ошибка сохранения состояния [{key}]: {e}")

        # Закрываем WebSocket если открыт
        if self.websocket:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(self.websocket.close())
            except:
                pass

        print("  ✅ Сборщик данных остановлен")

    # ---------- Обработка свечей ----------

    def process_kline_data(self, data):
        """Обработка данных свечи"""
        try:
            if "data" not in data:
                return

            kline_data = data["data"]

            if isinstance(kline_data, list):
                for item in kline_data:
                    if isinstance(item, dict):
                        self._process_single_candle(item)
                    elif isinstance(item, list):
                        self._process_single_candle_from_list(item)

            elif isinstance(kline_data, dict):
                self._process_single_candle(kline_data)

        except Exception as e:
            print(f"❌ ОШИБКА ОБРАБОТКИ KLINE: {e}")

    def _process_single_candle(self, candle_dict):
        """Обработка свечи в формате словаря (WebSocket)"""
        try:
            timestamp = int(candle_dict.get('start', candle_dict.get('t', 0)))
            open_price = float(candle_dict.get('open', candle_dict.get('o', 0)))
            high_price = float(candle_dict.get('high', candle_dict.get('h', 0)))
            low_price = float(candle_dict.get('low', candle_dict.get('l', 0)))
            close_price = float(candle_dict.get('close', candle_dict.get('c', 0)))
            volume = float(candle_dict.get('volume', candle_dict.get('v', 0)))
            confirm = candle_dict.get('confirm', candle_dict.get('x', False))

            if isinstance(confirm, str):
                confirm = confirm.lower() == 'true'

            self._update_candle_data(timestamp, open_price, high_price, low_price, close_price, volume, confirm)

        except Exception as e:
            print(f"❌ ОШИБКА ОБРАБОТКИ СВЕЧИ (dict): {e}")

    def _process_single_candle_from_list(self, candle_list):
        """Обработка свечи в формате списка (обычно REST)"""
        try:
            if len(candle_list) >= 5:
                timestamp = int(candle_list[0])
                open_price = float(candle_list[1])
                high_price = float(candle_list[2])
                low_price = float(candle_list[3])
                close_price = float(candle_list[4])
                volume = float(candle_list[5]) if len(candle_list) > 5 else 0.0
                confirm = True

                self._update_candle_data(timestamp, open_price, high_price, low_price, close_price, volume, confirm)

        except Exception as e:
            print(f"❌ ОШИБКА ОБРАБОТКИ СВЕЧИ (list): {e}")

    def _update_candle_data(self, timestamp, open_price, high_price, low_price, close_price, volume, confirm):
        """Обновление данных свечи в хранилище"""
        if timestamp == 0 or open_price == 0:
            return

        # Пока идёт переключение режима — игнорируем входящие данные.
        # Это защищает активную сессию от свечей старого endpoint.
        if self._reconnect_requested:
            return

        with self._data_lock:
            self._update_candle_data_locked(
                timestamp, open_price, high_price, low_price, close_price, volume, confirm
            )

    def _update_candle_data_locked(self, timestamp, open_price, high_price, low_price, close_price, volume, confirm):
        """Внутреннее обновление свечи (вызывать под _data_lock)."""
        session = self.session

        time_str = datetime.fromtimestamp(timestamp/1000).strftime('%H:%M:%S')
        change = close_price - open_price
        change_percent = (change / open_price) * 100 if open_price > 0 else 0
        status = "ЗАКРЫТА" if confirm else "ФОРМИРУЕТСЯ"

        print(f"🕯️ СВЕЧА [{status}] {time_str} "
              f"O:{open_price:.2f} H:{high_price:.2f} L:{low_price:.2f} C:{close_price:.2f} "
              f"Vol:{volume:.4f} ({change:+.2f} / {change_percent:+.2f}%)")

        candle_data = {
            'timestamp': timestamp,
            'open': open_price,
            'high': high_price,
            'low': low_price,
            'close': close_price,
            'volume': volume,
            'confirm': confirm,
        }

        # Признак новой свечи ДО мутации last_candle_time
        is_new_candle = (session.last_candle_time is None or timestamp > session.last_candle_time)

        if is_new_candle:
            self._finalize_candle(session.current_candle)
            session.last_candle_time = timestamp

        if confirm:
            self._finalize_candle(candle_data)
            session.current_candle = None
        else:
            session.current_candle = candle_data

    def _finalize_candle(self, candle):
        """
        Финализирует закрытую свечу активной сессии.
        Обновляет Kalman, OU и DemoTrader ровно на одно звено.
        """
        if candle is None:
            return

        session = self.session

        candle_entry = {
            'timestamp': candle['timestamp'],
            'open': candle['open'],
            'high': candle['high'],
            'low': candle['low'],
            'close': candle['close'],
            'volume': candle['volume'],
        }

        # Дубликат: обновляем OHLCV, индикаторы не трогаем
        if session.candles_data and session.candles_data[-1]['timestamp'] == candle['timestamp']:
            session.candles_data[-1] = candle_entry
            return

        session.candles_data.append(candle_entry)

        fair_price = session.kalman.update(candle['close'])
        session.kalman_estimates.append(fair_price)

        spread = candle['close'] - fair_price
        session.ou_signal = session.ou.update(spread)

        if session.ou_signal is not None:
            session.ou_z_history.append(session.ou_signal.get('z'))

        if session.ou_signal is not None:
            session.demo_trader.update(
                candle['close'],
                session.ou_signal,
                candle['timestamp'],
            )

    # ---------- Получение данных для отображения ----------

    def get_display_data(self):
        """Получение данных для отображения"""
        with self._data_lock:
            return self._build_display_candles()

    def _build_display_candles(self):
        """Сбор свечей для графика (вызывать под _data_lock)."""
        session = self.session
        display_candles = list(session.candles_data)

        if session.current_candle:
            if display_candles and display_candles[-1]['timestamp'] == session.current_candle['timestamp']:
                display_candles[-1] = session.current_candle
            else:
                display_candles.append(session.current_candle)
            if len(display_candles) > self.max_candles:
                display_candles = display_candles[-self.max_candles:]

        return display_candles

    def get_render_data(self):
        """Атомарно возвращает свечи и оценки Калмана одной длины."""
        with self._data_lock:
            display_candles = self._build_display_candles()
            estimates = self._build_kalman_estimates()
            return display_candles, estimates

    def _build_kalman_estimates(self):
        """Сбор оценок Калмана (вызывать под _data_lock)."""
        session = self.session
        estimates = list(session.kalman_estimates)

        if session.current_candle is not None:
            same_as_last = (
                session.candles_data
                and session.candles_data[-1]['timestamp'] == session.current_candle['timestamp']
            )
            if not same_as_last and estimates:
                estimates.append(estimates[-1])

        if len(estimates) > self.max_candles:
            estimates = estimates[-self.max_candles:]

        return estimates

    def get_last_candle(self):
        """Получение последней свечи"""
        display_candles = self.get_display_data()
        return display_candles[-1] if display_candles else None

    def get_status(self):
        """Получение статуса сборщика данных"""
        return {
            'is_running': self.is_running,
            'is_connected': self.is_connected,
            'symbol': self.symbol,
            'timeframe': self.interval,
            'candles_count': len(self.session.candles_data),
            'last_price': self.last_price,
            'market_mode': self.active_mode.key,
            'leverage': self.session.demo_trader.leverage,
        }

    def get_z_history(self) -> list:
        """Возвращает историю Z-score, выровненную по отображаемым свечам."""
        with self._data_lock:
            session = self.session
            z_values = list(session.ou_z_history)
            display_count = len(self._build_display_candles())
            if len(z_values) < display_count:
                z_values = [None] * (display_count - len(z_values)) + z_values
            elif len(z_values) > display_count:
                z_values = z_values[-display_count:]
            return z_values

    def get_trades_for_chart(self) -> list:
        """Возвращает список завершённых сделок в виде словарей."""
        with self._data_lock:
            return [trade.__dict__ for trade in self.session.demo_trader.trades]

    def get_kalman_estimates(self) -> list:
        """Возвращает оценки Калмана, выровненные по числу отображаемых свечей."""
        with self._data_lock:
            return self._build_kalman_estimates()

    def get_ou_status(self) -> dict:
        """Возвращает текущее состояние процесса OU."""
        with self._data_lock:
            return self.session.ou.get_status()

    def get_demo_trader_status(self) -> dict:
        """Возвращает текущее состояние демо-трейдера."""
        with self._data_lock:
            return self.session.demo_trader.get_status()