"""
Модуль веб-сервера
Отвечает за обслуживание HTTP запросов и WebSocket соединений с клиентами
"""

import json
import sys
import signal
from datetime import datetime
from flask import Flask, render_template_string, jsonify, request
from flask_socketio import SocketIO, emit

class WebServer:
    """Веб-сервер для отображения графиков"""
    
    def __init__(self, config, data_collector, chart_builder, template_manager):
        self.config = config
        self.data_collector = data_collector
        self.chart_builder = chart_builder
        self.template_manager = template_manager
        
        # Создание Flask приложения
        self.app = Flask(__name__)
        self.app.config['SECRET_KEY'] = config.SECRET_KEY
        
        # Инициализация SocketIO
        self.socketio = SocketIO(self.app, cors_allowed_origins="*")
        
        # Состояние сервера
        self.is_shutting_down = False
        self.shutdown_callback = None
        
        # Регистрация маршрутов и обработчиков
        self._register_routes()
        self._register_socket_events()
    
    def _register_routes(self):
        """Регистрация HTTP маршрутов"""
        
        @self.app.route('/')
        def index():
            """Главная страница"""
            template = self.template_manager.get_template('index.html')
            return render_template_string(
                template,
                symbol=self.config.SYMBOL,
                timeframe=self.data_collector.interval
            )
        
        @self.app.route('/api/chart_json')
        def get_chart_json():
            """API для получения графика в JSON"""
            display_candles, kalman_estimates = self.data_collector.get_render_data()
            chart_json = self.chart_builder.build_chart(
                display_candles,
                self.config.SYMBOL,
                self.data_collector.interval,
                kalman_estimates=kalman_estimates
            )
            if chart_json:
                return chart_json
            return jsonify({'error': 'No data available'}), 404
        
        @self.app.route('/api/stats')
        def get_stats():
            """API для получения статистики"""
            return jsonify({
                'high_24h': self.data_collector.high_24h,
                'low_24h': self.data_collector.low_24h,
                'volume_24h': self.data_collector.volume_24h,
                'last_price': self.data_collector.last_price,
                'candles_count': len(self.data_collector.candles_data),
                'is_connected': self.data_collector.is_connected,
                'timestamp': datetime.now().isoformat()
            })
        
        @self.app.route('/api/status')
        def get_status():
            """API для получения полного статуса"""
            return jsonify(self.data_collector.get_status())
        
        @self.app.route('/api/shutdown', methods=['POST'])
        def shutdown():
            """API для остановки сервера"""
            if not self.is_shutting_down:
                print("\n" + "="*60)
                print("🛑 ПОЛУЧЕН ЗАПРОС НА ОСТАНОВКУ СЕРВЕРА")
                print("="*60)
                
                self.is_shutting_down = True
                
                # Уведомляем всех клиентов
                self.socketio.emit('server_shutdown', {
                    'message': 'Сервер завершает работу...'
                })
                
                # Запускаем процесс остановки в отдельном потоке
                import threading
                shutdown_thread = threading.Thread(target=self._perform_shutdown)
                shutdown_thread.daemon = True
                shutdown_thread.start()
                
                return jsonify({
                    'status': 'shutting_down',
                    'message': 'Сервер завершает работу...'
                })
            
            return jsonify({
                'status': 'already_shutting_down',
                'message': 'Сервер уже завершает работу'
            })
    
    def _register_socket_events(self):
        """Регистрация WebSocket событий"""
        
        @self.socketio.on('connect')
        def handle_connect():
            """Обработка подключения клиента"""
            if not self.is_shutting_down:
                print(f'🔗 Клиент подключен')
                self._send_current_data()
        
        @self.socketio.on('disconnect')
        def handle_disconnect():
            """Обработка отключения клиента"""
            print(f'🔌 Клиент отключен')
    
    def _send_current_data(self):
        """Отправка текущих данных подключившемуся клиенту"""
        display_candles, kalman_estimates = self.data_collector.get_render_data()
        chart_json = self.chart_builder.build_chart(
            display_candles,
            self.config.SYMBOL,
            self.data_collector.interval,
            kalman_estimates=kalman_estimates
        )
        
        if chart_json and display_candles:
            last_candle = display_candles[-1]
            
            update_data = {
                'chart_json': chart_json,
                'last_price': last_candle['close'],
                'price_change': last_candle['close'] - last_candle['open'],
                'change_percent': ((last_candle['close'] - last_candle['open']) / last_candle['open']) * 100,
                'high_24h': self.data_collector.high_24h,
                'low_24h': self.data_collector.low_24h,
                'volume_24h': self.data_collector.volume_24h,
                'candles_count': len(self.data_collector.candles_data),
                'current_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'is_positive': last_candle['close'] >= last_candle['open'],
                'ou_status': self.data_collector.get_ou_status()
            }
            
            self.socketio.emit('chart_update', update_data)
    
    def broadcast_update(self):
        """Отправка обновления всем подключенным клиентам"""
        if self.is_shutting_down:
            return
            
        display_candles, kalman_estimates = self.data_collector.get_render_data()
        
        if not display_candles:
            return
        
        chart_json = self.chart_builder.build_chart(
            display_candles,
            self.config.SYMBOL,
            self.data_collector.interval,
            kalman_estimates=kalman_estimates
        )
        
        last_candle = display_candles[-1]
        
        update_data = {
            'chart_json': chart_json,
            'last_price': last_candle['close'],
            'price_change': last_candle['close'] - last_candle['open'],
            'change_percent': ((last_candle['close'] - last_candle['open']) / last_candle['open']) * 100,
            'high_24h': self.data_collector.high_24h,
            'low_24h': self.data_collector.low_24h,
            'volume_24h': self.data_collector.volume_24h,
            'candles_count': len(self.data_collector.candles_data),
            'current_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'is_positive': last_candle['close'] >= last_candle['open'],
            'ou_status': self.data_collector.get_ou_status()
        }
        
        self.socketio.emit('chart_update', update_data)
    
    def _perform_shutdown(self):
        """Инициирует мягкое завершение сервера через сигнал SIGTERM"""
        import time
        import os
        import signal

        print("⏳ Уведомление клиентов о завершении работы...")
        # Даём клиентам время получить уведомление
        self.socketio.emit('server_shutdown', {
            'message': 'Сервер завершает работу...'
        })
        time.sleep(1)

        # Останавливаем сборщик данных
        print("1️⃣ Остановка сборщика данных...")
        self.data_collector.shutdown()
        time.sleep(0.5)

        # Вместо жёсткого os._exit(0) отправляем сигнал сами себе
        print("2️⃣ Отправка сигнала завершения...")
        os.kill(os.getpid(), signal.SIGTERM)
    
    def run(self):
        """Запуск веб-сервера"""
        print("=" * 70)
        print("🚀 Веб-сервер запущен!")
        print(f"📊 Откройте браузер: http://localhost:{self.config.FLASK_PORT}")
        print(f"📈 График: {self.config.SYMBOL} ({self.data_collector.interval}min)")
        print(f"🛑 Для остановки используйте кнопку в веб-интерфейсе")
        print("=" * 70)
        
        self.socketio.run(
            self.app,
            host=self.config.FLASK_HOST,
            port=self.config.FLASK_PORT,
            debug=self.config.FLASK_DEBUG,
            use_reloader=False
        )