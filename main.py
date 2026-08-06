"""
Главный модуль приложения Bybit Realtime Chart
Точка входа для запуска всей системы
"""

import asyncio
import time
import signal
import sys
from threading import Thread

from config import Config
from data_collector import BybitDataCollector
from chart_builder import ChartBuilder
from web_server import WebServer
from template_manager import TemplateManager

class Application:
    """Главный класс приложения, объединяющий все компоненты"""
    
    def __init__(self):
        """Инициализация приложения"""
        print("=" * 70)
        print("🚀 Инициализация Bybit Realtime Chart")
        print("=" * 70)
        
        # Загрузка конфигурации
        self.config = Config()
        print(f"📋 Конфигурация загружена: {self.config.SYMBOL} ({self.config.TIMEFRAME}min)")
        
        # Инициализация компонентов
        self.template_manager = TemplateManager()
        self.data_collector = BybitDataCollector(self.config)
        self.chart_builder = ChartBuilder(self.config)
        self.web_server = WebServer(
            self.config,
            self.data_collector,
            self.chart_builder,
            self.template_manager
        )
        
        # Регистрируем callback завершения
        self.web_server.shutdown_callback = self._on_server_shutdown
        
        # Регистрируем обработчики сигналов
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
        print("✅ Все компоненты инициализированы")
    
    def _signal_handler(self, signum, frame):
        """Обработчик системных сигналов (Ctrl+C)"""
        print(f"\n⚠️ Получен сигнал завершения: {signal.Signals(signum).name}")
        self.shutdown()
    
    def _on_server_shutdown(self):
        """Callback при завершении сервера"""
        print("📡 Сервер завершает работу, очистка ресурсов...")
    
    def _run_websocket_client(self):
        """Запуск WebSocket клиента в отдельном потоке"""
        # Подключаем метод broadcast_update к сборщику данных
        original_update = self.data_collector._update_candle_data
        
        def update_with_broadcast(*args, **kwargs):
            result = original_update(*args, **kwargs)
            # После обновления данных отправляем их клиентам
            try:
                self.web_server.broadcast_update()
            except Exception as e:
                print(f"❌ Ошибка отправки обновления: {e}")
            return result
        
        self.data_collector._update_candle_data = update_with_broadcast
        
        while self.data_collector.is_running:
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(self.data_collector.connect_websocket())
            except Exception as e:
                if not self.data_collector.is_running:
                    break
                print(f"❌ Критическая ошибка WebSocket клиента: {e}")
                print("🔄 Перезапуск через 5 секунд...")
                time.sleep(5)
            finally:
                loop.close()
        
        print("✅ WebSocket поток завершен")
    
    def shutdown(self):
        """Корректное завершение работы всего приложения"""
        print("\n" + "="*60)
        print("🛑 ЗАВЕРШЕНИЕ РАБОТЫ ПРИЛОЖЕНИЯ")
        print("="*60)
        
        # Останавливаем сборщик данных
        print("📡 Отключение от Bybit WebSocket...")
        self.data_collector.shutdown()
        
        # Даем время на завершение
        time.sleep(1)
        
        print("✅ Приложение остановлено")
        sys.exit(0)
    
    def run(self):
        """Запуск приложения"""
        print("=" * 70)
        print("🚀 Запуск приложения")
        print("=" * 70)
        print("💡 Советы:")
        print("  - Для остановки нажмите кнопку 'Остановить' в веб-интерфейсе")
        print("  - Или нажмите Ctrl+C в этой консоли")
        print("=" * 70)
        
        # Запуск WebSocket клиента в фоновом потоке
        websocket_thread = Thread(target=self._run_websocket_client, daemon=True)
        websocket_thread.start()
        print("📡 WebSocket клиент запущен в фоновом режиме")
        
        # Запуск веб-сервера (блокирующий вызов)
        try:
            self.web_server.run()
        except KeyboardInterrupt:
            print("\n⚠️ Получен сигнал прерывания с клавиатуры")
            self.shutdown()

if __name__ == "__main__":
    app = Application()
    app.run()