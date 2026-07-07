"""
HTML-страница со статистикой обработки писем.

Генерирует красивый дашборд с графиками (Chart.js через CDN):
- Статистика за сегодня / за всё время
- Топ отправителей
- Недельный тепмáп (heatmap)
- График уверенности классификации
- Ошибки классификации по доменам

Открывается в браузере через python -m webbrowser.
"""

from __future__ import annotations

import os
import json
import webbrowser
from datetime import date, datetime
from typing import Any

from db import (
    get_daily_stats,
    get_total_stats,
    get_top_senders,
    get_stats_weekly,
    get_corrections_by_domain,
    get_history,
)


def _fmt(v: Any) -> str:
    """Безопасное строковое представление."""
    return str(v) if v is not None else ""


def generate_dashboard() -> str:
    """
    Генерирует HTML-файл со статистикой и возвращает путь к нему.
    """
    now = datetime.now()
    today = date.today()

    # Статистика
    daily = get_daily_stats(today)
    total = get_total_stats()
    top_senders = get_top_senders(limit=15)
    weekly = get_stats_weekly()
    corrections = get_corrections_by_domain(limit=10)

    # История для confidence-графика
    history = get_history(limit=200)

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Email Sorter — Статистика</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: #0f172a; color: #e2e8f0; padding: 24px; }}
  h1 {{ font-size: 28px; margin-bottom: 8px; color: #f8fafc; }}
  h2 {{ font-size: 20px; margin-bottom: 16px; color: #94a3b8; }}
  .subtitle {{ color: #64748b; margin-bottom: 24px; font-size: 14px; }}
  .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
                 gap: 16px; margin-bottom: 32px; }}
  .stat-card {{ background: #1e293b; border-radius: 12px; padding: 20px; }}
  .stat-card .value {{ font-size: 32px; font-weight: 700; color: #f8fafc; }}
  .stat-card .label {{ font-size: 13px; color: #64748b; margin-top: 4px; }}
  .stat-card.keep .value {{ color: #22c55e; }}
  .stat-card.delete .value {{ color: #ef4444; }}
  .stat-card.archive .value {{ color: #3b82f6; }}
  .stat-card.review .value {{ color: #eab308; }}
  .chart-container {{ background: #1e293b; border-radius: 12px; padding: 20px;
                     margin-bottom: 24px; }}
  .chart-container canvas {{ max-height: 350px; }}
  .charts-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px;
                 margin-bottom: 24px; }}
  @media (max-width: 800px) {{ .charts-row {{ grid-template-columns: 1fr; }} }}
  table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
  th {{ text-align: left; padding: 10px 12px; color: #64748b;
        border-bottom: 1px solid #334155; font-weight: 600; }}
  td {{ padding: 10px 12px; border-bottom: 1px solid #1e293b; }}
  tr:hover td {{ background: #1e293b; }}
  .badge {{ display: inline-block; padding: 2px 10px; border-radius: 6px;
            font-size: 12px; font-weight: 600; }}
  .badge.keep {{ background: #166534; color: #86efac; }}
  .badge.delete {{ background: #7f1d1d; color: #fca5a5; }}
  .badge.archive {{ background: #1e3a5f; color: #93c5fd; }}
  .badge.review {{ background: #713f12; color: #fde68a; }}
  .section {{ margin-bottom: 32px; }}
  .labels-grid {{ display: flex; flex-wrap: wrap; gap: 8px; }}
  .label-tag {{ background: #334155; padding: 4px 12px; border-radius: 8px;
                font-size: 13px; }}
  .footer {{ text-align: center; color: #475569; font-size: 12px;
             padding: 24px 0 8px; }}
</style>
</head>
<body>
<h1>📊 Email Sorter</h1>
<p class="subtitle">Статистика обработки писем · {now.strftime('%d.%m.%Y %H:%M')}</p>

<!-- Статистика за сегодня -->
<h2>📅 Сегодня ({today.strftime('%d.%m.%Y')})</h2>
<div class="stats-grid">
  <div class="stat-card"><div class="value">{daily['total']}</div><div class="label">Всего обработано</div></div>
  <div class="stat-card keep"><div class="value">{daily['deleted']}</div><div class="label">🗑️ Удалено</div></div>
  <div class="stat-card archive"><div class="value">{daily['archived']}</div><div class="label">📦 В архиве</div></div>
  <div class="stat-card keep"><div class="value">{daily['kept']}</div><div class="label">✅ Сохранено</div></div>
  <div class="stat-card review"><div class="value">{daily['reviewed']}</div><div class="label">🤔 На проверке</div></div>
  <div class="stat-card"><div class="value">{daily['low_confidence']}</div><div class="label">⚡ Низкая уверенность</div></div>
</div>

<!-- Метки за сегодня -->
<div class="section">
  <h2>🏷️ Метки писем</h2>
  <div class="labels-grid">
    {''.join(f'<span class="label-tag"><strong>{k}:</strong> {v}</span>' for k, v in sorted(daily['labels'].items()))}
  </div>
</div>

<!-- Общая статистика -->
<h2>📈 За всё время</h2>
<div class="stats-grid">
  <div class="stat-card"><div class="value">{total['total']}</div><div class="label">Всего обработано</div></div>
  <div class="stat-card delete"><div class="value">{total['deleted']}</div><div class="label">🗑️ Удалено</div></div>
  <div class="stat-card archive"><div class="value">{total['archived']}</div><div class="label">📦 В архиве</div></div>
  <div class="stat-card keep"><div class="value">{total['kept']}</div><div class="label">✅ Сохранено</div></div>
  <div class="stat-card review"><div class="value">{total['reviewed']}</div><div class="label">🤔 На проверке</div></div>
</div>

<!-- Графики -->
<div class="charts-row">
  <!-- Недельная динамика -->
  <div class="chart-container">
    <h2>📆 Недельная динамика</h2>
    <canvas id="weeklyChart"></canvas>
  </div>
  <!-- Пирог: решения за всё время -->
  <div class="chart-container">
    <h2>🥧 Соотношение решений</h2>
    <canvas id="pieChart"></canvas>
  </div>
</div>

<div class="charts-row">
  <!-- Уверенность классификации -->
  <div class="chart-container">
    <h2>⚡ Уверенность классификации (последние 100)</h2>
    <canvas id="confidenceChart"></canvas>
  </div>
  <!-- Топ отправителей -->
  <div class="chart-container">
    <h2>📧 Топ отправителей</h2>
    <canvas id="sendersChart"></canvas>
  </div>
</div>

<!-- Ошибки классификации -->
<div class="section">
  <h2>🔄 Домены с частыми коррекциями</h2>
  <table>
    <tr><th>Домен</th><th>Кол-во коррекций</th><th>Было решений</th></tr>
    {''.join(f'<tr><td>{_fmt(r["domain"])}</td><td>{_fmt(r["cnt"])}</td><td>{_fmt(r.get("wrong_decisions", ""))}</td></tr>' for r in corrections) if corrections else '<tr><td colspan="3" style="color:#64748b;">Нет данных</td></tr>'}
  </table>
</div>

<!-- Топ отправителей (таблица) -->
<div class="section">
  <h2>👤 Таблица отправителей</h2>
  <table>
    <tr><th>Отправитель</th><th>Писем</th><th>Удалено</th><th>% удаления</th></tr>
    {''.join(
      f'<tr><td>{_fmt(r["sender"])}</td><td>{_fmt(r["cnt"])}</td>'
      f'<td>{_fmt(r["deleted_cnt"])}</td>'
      f'<td>{"{:.0%}".format(r["deleted_cnt"]/r["cnt"]) if r["cnt"] > 0 else "—"}</td></tr>'
      for r in top_senders
    )}
  </table>
</div>

<div class="footer">
  Email Sorter · данные из SQLite · обновлено {now.strftime('%d.%m.%Y %H:%M')}
</div>

<script>
// Недельная динамика
new Chart(document.getElementById('weeklyChart'), {{
  type: 'bar',
  data: {{
    labels: {json.dumps([w.get('day','') for w in weekly])},
    datasets: [{{
      label: 'Всего',
      data: {json.dumps([w.get('total',0) for w in weekly])},
      backgroundColor: '#3b82f6',
      borderRadius: 4,
    }}, {{
      label: 'Удалено',
      data: {json.dumps([w.get('deleted',0) for w in weekly])},
      backgroundColor: '#ef4444',
      borderRadius: 4,
    }}]
  }},
  options: {{
    responsive: true,
    plugins: {{ legend: {{ labels: {{ color: '#94a3b8' }} }} }},
    scales: {{ x: {{ ticks: {{ color: '#64748b' }} }}, y: {{ ticks: {{ color: '#64748b' }} }} }}
  }}
}});

// Пирог
new Chart(document.getElementById('pieChart'), {{
  type: 'doughnut',
  data: {{
    labels: ['🗑️ Удалено ({total['deleted']})', '📦 Архив ({total['archived']})', '✅ Сохранено ({total['kept']})', '🤔 Проверка ({total['reviewed']})'],
    datasets: [{{
      data: [{total['deleted']}, {total['archived']}, {total['kept']}, {total['reviewed']}],
      backgroundColor: ['#ef4444', '#3b82f6', '#22c55e', '#eab308'],
      borderWidth: 0,
    }}]
  }},
  options: {{
    responsive: true,
    plugins: {{ legend: {{ position: 'bottom', labels: {{ color: '#94a3b8' }} }} }}
  }}
}});

// Уверенность
const confData = {json.dumps([h.get('confidence',0) for h in history[-100:]])};
const confLabels = confData.map((_,i) => i+1);
new Chart(document.getElementById('confidenceChart'), {{
  type: 'line',
  data: {{
    labels: confLabels,
    datasets: [{{
      label: 'Уверенность',
      data: confData,
      borderColor: '#a855f7',
      backgroundColor: 'rgba(168,85,247,0.1)',
      fill: true,
      tension: 0.3,
      pointRadius: 2,
    }}]
  }},
  options: {{
    responsive: true,
    plugins: {{ legend: {{ labels: {{ color: '#94a3b8' }} }} }},
    scales: {{
      x: {{ ticks: {{ color: '#64748b' }}, title: {{ display: true, text: 'Письмо #', color: '#64748b' }} }},
      y: {{ min: 0, max: 1, ticks: {{ color: '#64748b', callback: v => (v*100).toFixed(0)+'%' }} }}
    }}
  }}
}});

// Топ отправителей
new Chart(document.getElementById('sendersChart'), {{
  type: 'bar',
  data: {{
    labels: {json.dumps([r['sender'][:25] for r in top_senders[:10]])},
    datasets: [{{
      label: 'Писем',
      data: {json.dumps([r['cnt'] for r in top_senders[:10]])},
      backgroundColor: '#3b82f6',
      borderRadius: 4,
    }}]
  }},
  options: {{
    indexAxis: 'y',
    responsive: true,
    plugins: {{ legend: {{ labels: {{ color: '#94a3b8' }} }} }},
    scales: {{ x: {{ ticks: {{ color: '#64748b' }} }}, y: {{ ticks: {{ color: '#64748b' }} }} }}
  }}
}});
</script>
</body>
</html>"""

    # Сохраняем
    stats_dir = _get_stats_dir()
    os.makedirs(stats_dir, exist_ok=True)
    filepath = os.path.join(stats_dir, "dashboard.html")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)

    return filepath


def _get_stats_dir() -> str:
    """Возвращает директорию для HTML-статистики."""
    return os.path.join(os.path.dirname(__file__), "data", "stats")


def open_dashboard() -> None:
    """Генерирует дашборд и открывает в браузере."""
    filepath = generate_dashboard()
    webbrowser.open(f"file://{filepath}")
    print(f"📊 Дашборд открыт: {filepath}")


def show_link() -> None:
    """Генерирует и показывает путь к дашборду."""
    filepath = generate_dashboard()
    print(f"📊 Дашборд: file://{filepath}")
