"""
Модуль создания графиков Plotly
Отвечает за визуализацию данных на серверной стороне
"""

import numpy as np
from datetime import datetime
import plotly.graph_objects as go
from plotly.subplots import make_subplots

class ChartBuilder:
    """Построитель графиков на основе Plotly"""
    
    def __init__(self, config):
        """
        Инициализация построителя графиков
        
        Args:
            config: Объект конфигурации
        """
        self.config = config
        self.colors = config.COLORS
    
    def build_chart(self, display_candles, symbol, interval,kalman_estimates=None, z_history=None, trades=None, open_position=None):
        """
        Создание графика Plotly
        
        Args:
            display_candles: Список свечей для отображения
            symbol: Торговая пара
            interval: Таймфрейм
        
        Returns:
            JSON представление графика Plotly
        """
        if not display_candles:
            return None
        
        # Извлечение данных
        timestamps = [datetime.fromtimestamp(c['timestamp'] / 1000).strftime('%H:%M:%S') 
                     for c in display_candles]
        opens = [c['open'] for c in display_candles]
        highs = [c['high'] for c in display_candles]
        lows = [c['low'] for c in display_candles]
        closes = [c['close'] for c in display_candles]
        
        # Создание subplot (свечи + объем)
        fig = make_subplots(
            rows=2, cols=1,
            shared_xaxes=True,
            vertical_spacing=0.03,
            row_heights=[0.7, 0.3]
        )
        
        # Добавление свечного графика с Z-score в подсказке
        self._add_candlestick(
            fig, timestamps, opens, highs, lows, closes, z_history
        )

        # Фильтруем сделки, попадающие в текущее окно отображения
        if trades and display_candles:
            min_ts = display_candles[0]['timestamp']
            max_ts = display_candles[-1]['timestamp']

            # Оставляем сделки, у которых время входа или выхода внутри окна
            visible_trades = [
                t for t in trades
                if (min_ts <= t['entry_time'] <= max_ts) or
                   (min_ts <= t['exit_time'] <= max_ts)
            ]

            if visible_trades:
                self._add_trade_markers(fig, visible_trades)

        # Добавляем маркер открытой позиции
        if open_position is not None and display_candles:
            self._add_open_position_marker(fig, open_position, display_candles)
        
        # Добавление скользящих средних
        if self.config.ENABLE_SMA_5:
            self._add_sma(fig, timestamps, closes, 5, self.colors["sma_5"], "SMA 5")
        
        if self.config.ENABLE_SMA_10:
            self._add_sma(fig, timestamps, closes, 10, self.colors["sma_10"], "SMA 10")
        
        # Добавление объема (реальные данные)
        self._add_volume(fig, timestamps, display_candles)

        if kalman_estimates is not None and len(kalman_estimates) > 0:
            if len(kalman_estimates) != len(timestamps):
                if len(kalman_estimates) > len(timestamps):
                    kalman_estimates = kalman_estimates[-len(timestamps):]
                else:
                    pad_value = kalman_estimates[-1]
                    kalman_estimates = kalman_estimates + [pad_value] * (len(timestamps) - len(kalman_estimates))
            self._add_kalman_line(fig, timestamps, kalman_estimates)

        # Добавляем невидимый трейс для отображения Z-score в hover
        if z_history is not None and len(z_history) == len(timestamps):
            self._add_z_score_hover_trace(fig, timestamps, closes, z_history)
        
        # Настройка макета
        self._apply_layout(fig, symbol, interval)
        
        return fig.to_json()
    
    def _add_candlestick(self, fig, timestamps, opens, highs, lows, closes, z_history=None):
        """Добавление свечного графика с возможным отображением Z-score."""
        # Подготовка customdata для Z-score
        customdata = None
        hovertemplate = None

        if z_history is not None and len(z_history) == len(timestamps):
            customdata = np.array(z_history).reshape(-1, 1)
            hovertemplate = (
                'Time: %{x}<br>'
                'Open: %{open:.2f}<br>'
                'High: %{high:.2f}<br>'
                'Low: %{low:.2f}<br>'
                'Close: %{close:.2f}<br>'
                'Z-score: %{customdata[0]:.2f}<extra></extra>'
            )

        fig.add_trace(
            go.Candlestick(
                x=timestamps,
                open=opens,
                high=highs,
                low=lows,
                close=closes,
                name='Price',
                increasing=dict(
                    line=dict(color=self.colors["bullish"], width=2),
                    fillcolor=f'rgba(0, 255, 136, 0.7)'
                ),
                decreasing=dict(
                    line=dict(color=self.colors["bearish"], width=2),
                    fillcolor=f'rgba(255, 68, 68, 0.7)'
                ),
                whiskerwidth=0.2,
                showlegend=False,
                uid='price-candles',
                customdata=customdata,
                hovertemplate=hovertemplate
            ),
            row=1, col=1
        )
    
    def _add_sma(self, fig, timestamps, closes, period, color, name):
        """Добавление скользящей средней"""
        if len(closes) < period:
            return
        
        sma_values = [sum(closes[max(0, i-period+1):i+1]) / min(i+1, period) 
                     for i in range(len(closes))]
        
        fig.add_trace(
            go.Scatter(
                x=timestamps,
                y=sma_values,
                mode='lines',
                name=name,
                line=dict(color=color, width=1.5)
            ),
            row=1, col=1
        )
    
    def _add_volume(self, fig, timestamps, display_candles):
        """Добавление графика объема на основе реальных данных"""
        # Извлекаем объемы из данных свечей
        volumes = [c.get('volume', 0) for c in display_candles]
        
        # Определяем цвета столбцов: зеленый для бычьих свечей, красный для медвежьих
        volume_colors = [
            self.colors["bullish"] if c['close'] >= c['open'] 
            else self.colors["bearish"] 
            for c in display_candles
        ]
        
        fig.add_trace(
            go.Bar(
                x=timestamps,
                y=volumes,
                name='Volume',
                marker=dict(color=volume_colors, opacity=0.6),
                showlegend=False
            ),
            row=2, col=1
        )
    
    def _apply_layout(self, fig, symbol, interval):
        """Применение настроек макета"""
        fig.update_layout(
            template=self.config.CHART_TEMPLATE,
            paper_bgcolor=f'rgba(26, 26, 46, 0.95)',
            plot_bgcolor=f'rgba(22, 33, 62, 0.8)',
            title={
                'text': f'{symbol}/USDT - {interval}min Realtime',
                'x': 0.5,
                'font': {'size': 20, 'color': self.colors["text"]}
            },
            xaxis=dict(
                gridcolor=self.colors["grid"],
                rangeslider=dict(visible=False)
            ),
            yaxis=dict(
                title='Price (USDT)',
                gridcolor=self.colors["grid"],
                side='right'
            ),
            yaxis2=dict(
                title='Volume',
                gridcolor=self.colors["grid"]
            ),
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="right",
                x=1,
                bgcolor='rgba(0, 0, 0, 0.5)',
                font=dict(color=self.colors["text"])
            ),
            hovermode='x unified',
            margin=dict(l=60, r=60, t=80, b=50),
            height=self.config.CHART_HEIGHT,
            uirevision='bybit-realtime-chart'
        )

    def _add_kalman_line(self, fig, x, y):
        """Добавляет линию фильтра Калмана на график."""
        fig.add_trace(
            go.Scatter(
                x=x,
                y=y,
                mode='lines',
                name='Kalman Fair Price',
                line=dict(color='#ffff00', width=2, dash='solid'),
                showlegend=True,
                uid='kalman-fair-price'
            ),
            row=1, col=1
        )

    def _add_trade_markers(self, fig, trades):
        """Добавляет на график точки входа и выхода из сделок."""
        entry_times, entry_prices, entry_colors, entry_symbols, entry_texts = [], [], [], [], []
        exit_times, exit_prices, exit_colors, exit_texts = [], [], [], []

        for trade in trades:
            try:
                entry_dt = datetime.fromtimestamp(trade['entry_time'] / 1000).strftime('%H:%M:%S')
                exit_dt = datetime.fromtimestamp(trade['exit_time'] / 1000).strftime('%H:%M:%S')
                direction = trade['direction']
                reason = trade['reason']
                pnl = trade['pnl']

                # Вход
                entry_times.append(entry_dt)
                entry_prices.append(trade['entry_price'])
                entry_colors.append('#00ff88' if direction == 'BUY' else '#ff4444')
                entry_symbols.append('triangle-up' if direction == 'BUY' else 'triangle-down')
                entry_texts.append(f"Вход {direction} @ {trade['entry_price']:.2f}")

                # Выход
                exit_times.append(exit_dt)
                exit_prices.append(trade['exit_price'])
                exit_colors.append('#00ff88' if pnl >= 0 else '#ff4444')
                exit_texts.append(f"Выход {reason} @ {trade['exit_price']:.2f} (PnL {pnl:+.2f})")
            except Exception:
                continue

        # Добавляем точки входа
        if entry_times:
            fig.add_trace(
                go.Scatter(
                    x=entry_times,
                    y=entry_prices,
                    mode='markers',
                    marker=dict(
                        color=entry_colors,
                        symbol=entry_symbols,
                        size=12,
                        line=dict(width=1, color='white')
                    ),
                    text=entry_texts,
                    hoverinfo='text',
                    showlegend=False
                ),
                row=1, col=1
            )

        # Добавляем точки выхода
        if exit_times:
            fig.add_trace(
                go.Scatter(
                    x=exit_times,
                    y=exit_prices,
                    mode='markers',
                    marker=dict(
                        color=exit_colors,
                        symbol='circle',
                        size=10,
                        line=dict(width=1, color='white')
                    ),
                    text=exit_texts,
                    hoverinfo='text',
                    showlegend=False
                ),
                row=1, col=1
            )

    def _add_open_position_marker(self, fig, position, display_candles):
        """Добавляет маркер входа для текущей открытой позиции."""
        try:
            entry_time = position.get('entry_time')
            entry_price = position.get('entry_price')
            direction = position.get('direction')

            if entry_time is None or entry_price is None:
                return

            # Проверяем, попадает ли вход в видимое окно
            min_ts = display_candles[0]['timestamp']
            max_ts = display_candles[-1]['timestamp']
            if not (min_ts <= entry_time <= max_ts):
                return

            entry_dt = datetime.fromtimestamp(entry_time / 1000).strftime('%H:%M:%S')
            color = '#00ff88' if direction == 'BUY' else '#ff4444'
            symbol = 'triangle-up' if direction == 'BUY' else 'triangle-down'

            fig.add_trace(
                go.Scatter(
                    x=[entry_dt],
                    y=[entry_price],
                    mode='markers',
                    marker=dict(
                        color=color,
                        symbol=symbol,
                        size=14,
                        line=dict(width=2, color='yellow')  # выделим открытую позицию
                    ),
                    text=[f"Открыта {direction} @ {entry_price:.2f}"],
                    hoverinfo='text',
                    showlegend=False
                ),
                row=1, col=1
            )
        except Exception as e:
            print(f"Ошибка отображения открытой позиции: {e}")

    def _add_z_score_hover_trace(self, fig, x, y, z_values):
        """Добавляет невидимый трейс, который показывает Z-score в hover."""
        # Используем closes как y-координаты (чтобы точки совпадали с графиком)
        hovertemplate = 'Z-score: %{customdata:.2f}<extra></extra>'

        fig.add_trace(
            go.Scatter(
                x=x,
                y=y,
                mode='markers',
                marker=dict(size=0, opacity=0),
                customdata=np.array(z_values).reshape(-1, 1),
                hovertemplate=hovertemplate,
                showlegend=False,
                hoverinfo='text',
                text=[f"Z-score: {z:.2f}" if z is not None else "Z-score: --" for z in z_values],
                uid='z-score-hover'
            ),
            row=1, col=1
        )