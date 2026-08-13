"""
Модуль сбора данных с Bybit
Отвечает за подключение к WebSocket и получение свечей в реальном времени
"""

import asyncio
import json
import time
import ssl
from datetime import datetime
from collections import deque
from threading import RLock
import websockets
import requests
import urllib3

from kalman_filter import KalmanFilter

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class BybitDataCollector:
    """Сборщик данных с биржи Bybit"""
    
    def __init__(self, config):
        self.config = config
        self.symbol = config.SYMBOL
        self.timeframe = config.TIMEFRAME
        self.max_candles = config.MAX_CANDLES
        self.interval = config.format_timeframe(config.TIMEFRAME)
        
        # Хранилище данных
        self.candles_data = deque(maxlen=self.max_candles)
        self.current_candle = None
        self.last_candle_time = None
        
        # Статистика 24ч
        self.high_24h = 0
        self.low_24h = float('inf')
        self.volume_24h = 0
        self.last_price = 0
        
        # Состояние
        self.is_running = True
        self.is_connected = False
        self.ssl_context = config.create_ssl_context()
        
        # WebSocket соединение
        self.websocket = None

        # Фильтр Калмана и хранение оценок
        self.kalman = KalmanFilter(
            delta_t=config.KALMAN_DELTA_T,
            q_level=config.KALMAN_Q_LEVEL,
            q_trend=config.KALMAN_Q_TREND,
            r=config.KALMAN_R
        )
        self.kalman_estimates = deque(maxlen=self.max_candles)
        self._data_lock = RLock()
        
        # Загрузка начальных данных
        self.fetch_initial_candles()
        self.fetch_24h_stats()
    
    def fetch_initial_candles(self):
        """Получение начальных исторических данных через REST API"""
        try:
            url = f"{self.config.BYBIT_REST_API}/kline"
            params = {
                "category": "spot",
                "symbol": self.symbol,
                "interval": self.interval,
                "limit": self.max_candles
            }
            
            print(f"\n{'='*60}")
            print(f"📡 ЗАПРОС ИСТОРИЧЕСКИХ ДАННЫХ")
            print(f"{'='*60}")
            print(f"URL: {url}")
            print(f"Пара: {self.symbol}")
            print(f"Таймфрейм: {self.interval}min")
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
                    candles.sort(key=lambda x: int(x[0]) if isinstance(x, list) else int(x.get('start', x.get('timestamp', 0))))

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
                            'volume': volume
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

                    # Последняя свеча из REST — текущая незакрытая минута; в историю не кладём.
                    closed_candles = parsed_candles[:-1] if parsed_candles else []
                    for candle in closed_candles:
                        self.candles_data.append(candle)

                    if parsed_candles:
                        last_candle = parsed_candles[-1]
                        self.current_candle = {**last_candle, 'confirm': False}
                        self.last_candle_time = last_candle['timestamp']

                    print(f"\n✅ ЗАГРУЖЕНО {len(self.candles_data)} ЗАКРЫТЫХ СВЕЧЕЙ")
                    if self.current_candle:
                        print(f"⏳ Текущая формирующаяся свеча вынесена отдельно")
            else:
                print(f"❌ ОШИБКА API: {data.get('retMsg', 'Unknown error')}")

            # Инициализация фильтра Калмана историческими данными
            if len(self.candles_data) > 0:
                for candle in self.candles_data:
                    fair_price = self.kalman.update(candle['close'])
                    self.kalman_estimates.append(fair_price)
                print(f"📈 Фильтр Калмана инициализирован {len(self.kalman_estimates)} оценками")
                
        except Exception as e:
            print(f"❌ ОШИБКА при получении начальных данных: {e}")
            import traceback
            traceback.print_exc()
    
    def fetch_24h_stats(self):
        """Получение 24-часовой статистики"""
        try:
            url = f"{self.config.BYBIT_REST_API}/tickers"
            params = {
                "category": "spot",
                "symbol": self.symbol
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
                
                print(f"📊 24Ч СТАТИСТИКА:")
                print(f"  Максимум: ${self.high_24h:,.2f}")
                print(f"  Минимум:  ${self.low_24h:,.2f}")
                print(f"  Объем:    {self.volume_24h:,.2f}")
                print(f"  Последняя цена: ${self.last_price:,.2f}")
                
        except Exception as e:
            print(f"❌ Ошибка получения 24ч статистики: {e}")
    
    async def connect_websocket(self):
        """Подключение к WebSocket Bybit и получение данных"""
        uri = self.config.BYBIT_WS_MAIN
        
        print(f"\n{'='*60}")
        print(f"📡 ПОДКЛЮЧЕНИЕ К WEBSOCKET")
        print(f"{'='*60}")
        print(f"URI: {uri}")
        
        while self.is_running:
            try:
                async with websockets.connect(
                    uri,
                    ssl=self.ssl_context,
                    ping_interval=self.config.WS_PING_INTERVAL,
                    ping_timeout=self.config.WS_PING_TIMEOUT,
                    close_timeout=5
                ) as websocket:
                    
                    self.websocket = websocket
                    self.is_connected = True
                    
                    print(f"✅ WEBSOCKET ПОДКЛЮЧЕН")
                    
                    # Подписка на свечи
                    subscribe_msg = {
                        "op": "subscribe",
                        "args": [f"kline.{self.interval}.{self.symbol}"]
                    }
                    await websocket.send(json.dumps(subscribe_msg))
                    
                    print(f"📡 Подписка на kline.{self.interval}.{self.symbol}")
                    print(f"  Ожидание данных...\n")
                    
                    while self.is_running:
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
                
            print(f"\n🔄 ПЕРЕПОДКЛЮЧЕНИЕ через {self.config.WS_RECONNECT_DELAY} секунд...")
            await asyncio.sleep(self.config.WS_RECONNECT_DELAY)
        
        print("✅ WebSocket клиент завершил работу")
    
    async def _unsubscribe(self, websocket):
        """Отписка от каналов при завершении"""
        try:
            unsubscribe_msg = {
                "op": "unsubscribe",
                "args": [f"kline.{self.interval}.{self.symbol}"]
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
        
        # Закрываем WebSocket если открыт
        if self.websocket:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(self.websocket.close())
            except:
                pass
        
        print("  ✅ Сборщик данных остановлен")
    
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
            # start — время открытия свечи (ключ минуты); timestamp — время последней сделки.
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
                confirm = True  # В списке обычно приходят уже закрытые свечи
                
                self._update_candle_data(timestamp, open_price, high_price, low_price, close_price, volume, confirm)
                
        except Exception as e:
            print(f"❌ ОШИБКА ОБРАБОТКИ СВЕЧИ (list): {e}")
    
    def _update_candle_data(self, timestamp, open_price, high_price, low_price, close_price, volume, confirm):
        """Обновление данных свечи в хранилище"""
        if timestamp == 0 or open_price == 0:
            return

        with self._data_lock:
            self._update_candle_data_locked(
                timestamp, open_price, high_price, low_price, close_price, volume, confirm
            )

    def _update_candle_data_locked(self, timestamp, open_price, high_price, low_price, close_price, volume, confirm):
        """Внутреннее обновление свечи (вызывать под _data_lock)."""
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
            'confirm': confirm
        }
        
        # Фиксируем признак новой свечи ДО мутации last_candle_time
        is_new_candle = (self.last_candle_time is None or timestamp > self.last_candle_time)

        if is_new_candle:
            # Началась новая свеча: если предыдущая формирующаяся свеча
            # ещё не была закрыта явным confirm=True, финализируем её
            # (закрываем и добавляем +1 звено фильтра Калмана).
            self._finalize_candle(self.current_candle)
            self.last_candle_time = timestamp

        if confirm:
            # Свеча закрыта: добавляем её в историю и обновляем фильтр
            # Калмана ровно на одно звено. На тики внутри незакрытой свечи
            # фильтр не реагирует (delta_t=1.0 рассчитан на один шаг на свечу).
            self._finalize_candle(candle_data)
            self.current_candle = None
        else:
            # Свеча ещё формируется — держим её отдельно, фильтр не трогаем.
            self.current_candle = candle_data
    
    def _finalize_candle(self, candle):
        """
        Финализирует закрытую свечу: добавляет её в историю и обновляет
        фильтр Калмана ровно на одно звено (+1 оценка справедливой цены).

        Вызывается либо при получении confirm=True для текущей свечи,
        либо при появлении новой свечи, если предыдущая ещё не была закрыта.
        Дубликаты (повторный confirm той же свечи) игнорируются.
        """
        if candle is None:
            return

        candle_entry = {
            'timestamp': candle['timestamp'],
            'open': candle['open'],
            'high': candle['high'],
            'low': candle['low'],
            'close': candle['close'],
            'volume': candle['volume']
        }

        # Свеча с таким start уже в истории — обновляем OHLCV, Калман не трогаем.
        if self.candles_data and self.candles_data[-1]['timestamp'] == candle['timestamp']:
            self.candles_data[-1] = candle_entry
            return

        self.candles_data.append(candle_entry)

        fair_price = self.kalman.update(candle['close'])
        self.kalman_estimates.append(fair_price)

    def get_display_data(self):
        """Получение данных для отображения"""
        with self._data_lock:
            return self._build_display_candles()

    def _build_display_candles(self):
        """Сбор свечей для графика (вызывать под _data_lock)."""
        display_candles = list(self.candles_data)

        if self.current_candle:
            if display_candles and display_candles[-1]['timestamp'] == self.current_candle['timestamp']:
                display_candles[-1] = self.current_candle
            else:
                display_candles.append(self.current_candle)
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
        estimates = list(self.kalman_estimates)

        if self.current_candle is not None:
            same_as_last = (
                self.candles_data
                and self.candles_data[-1]['timestamp'] == self.current_candle['timestamp']
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
            'candles_count': len(self.candles_data),
            'last_price': self.last_price
        }

    def get_kalman_estimates(self) -> list:
        """Возвращает оценки Калмана, выровненные по числу отображаемых свечей."""
        with self._data_lock:
            return self._build_kalman_estimates()