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
    
    def build_chart(self, display_candles, symbol, interval, kalman_estimates=None):
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
        
        # Добавление свечного графика
        self._add_candlestick(fig, timestamps, opens, highs, lows, closes)
        
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
        
        # Настройка макета
        self._apply_layout(fig, symbol, interval)
        
        return fig.to_json()
    
    def _add_candlestick(self, fig, timestamps, opens, highs, lows, closes):
        """Добавление свечного графика"""
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
                uid='price-candles'
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