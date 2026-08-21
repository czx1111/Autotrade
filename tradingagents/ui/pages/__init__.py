"""Web UI 页面模块：dashboard / studio / positions / screener / chart / config / reports / health / alerts。"""

from . import alerts, chart, config, dashboard, health, positions, reports, screener, studio

PAGES = {
    "📊 总览仪表盘": dashboard.render,
    "🤖 智能体工作室": studio.render,
    "📈 持仓与交易": positions.render,
    "🔍 发现选股": screener.render,
    "📐 K线看盘": chart.render,
    "🩺 系统健康": health.render,
    "🔔 告警中心": alerts.render,
    "⚙️ 策略配置": config.render,
    "📋 历史报告": reports.render,
}

__all__ = ["PAGES"]
