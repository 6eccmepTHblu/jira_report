#!/usr/bin/env python3
"""Рендерит нормализованный Jira-report-data в самодостаточный HTML.

Перенесено из скилла jira-browser-workflow (MIT, Hermes Agent + Nous Research), чтобы приложение работало самостоятельно.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


TEMPLATE_PATH = Path(__file__).resolve().parent / "assets" / "report_template.html"
BODY_MARKER = '<div class="shell">'
DATA_OPEN = '<script type="application/json" id="report-data">'
DATA_CLOSE = "</script>"
STATUS_META = {
    "backlog": ("Очередь", "#6b7280"),
    "progress": ("В работе", "#2f78ff"),
    "paused": ("Приостановлено", "#f0a21a"),
    "review": ("Проверка", "#8b6cff"),
    "done": ("Завершено", "#18a875"),
}
# Геометрия графика веток: viewBox фиксирован, узкий экран прокручивает контейнер.
GRAPH_WIDTH = 1100
GRAPH_LEFT = 96
GRAPH_RIGHT = 170
GRAPH_ROW = 32
GRAPH_TOP = 30
CHIP_STYLE = {
    "backlog": "--chip-bg:var(--paper-alt);--chip:var(--muted)",
    "progress": "--chip-bg:var(--accent-soft);--chip:var(--accent-strong)",
    "paused": "--chip-bg:var(--paused-soft);--chip:var(--paused)",
    "review": "--chip-bg:#eeeaff;--chip:#6847d8",
    "done": "--chip-bg:var(--success-soft);--chip:var(--success)",
}

def esc(value: Any) -> str:
    """Экранирует значение для HTML-текста и атрибутов."""
    return html.escape(str(value if value is not None else ""), quote=True)


def plural(count: int, one: str, few: str, many: str) -> str:
    """Выбирает русскую форму существительного."""
    value = abs(int(count))
    if value % 10 == 1 and value % 100 != 11:
        return one
    if value % 10 in (2, 3, 4) and value % 100 not in (12, 13, 14):
        return few
    return many


def format_seconds(value: Any, include_seconds: bool = False) -> str:
    """Форматирует секунды без ручного пересчёта в шаблоне."""
    seconds = max(0, int(value or 0))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    parts = []
    if hours:
        parts.append("%d ч" % hours)
    if minutes or (hours and not seconds):
        parts.append("%d мин" % minutes)
    if include_seconds and seconds:
        parts.append("%d с" % seconds)
    if not parts:
        return "0 с" if include_seconds else "0 мин"
    return " ".join(parts)


# Jira отдаёт время в поясе своего профиля (у ATOM это +05), а период отчёта
# считается в `period.timezone`. Без приведения карточки показывали чужие часы:
# ночная запись 23:52 по Москве выглядела как 01:52 следующего дня.
_DISPLAY_TZ: tzinfo | None = None


def parse_datetime(value: Any) -> datetime | None:
    """Разбирает Jira datetime для локального отображения."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if len(text) >= 5 and text[-5] in "+-" and text[-3] != ":":
        text = text[:-2] + ":" + text[-2:]
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def format_datetime(value: Any, with_year: bool = False) -> str:
    """Форматирует дату события в часовом поясе отчёта."""
    parsed = parse_datetime(value)
    if not parsed:
        return "—"
    if _DISPLAY_TZ is not None and parsed.tzinfo is not None:
        parsed = parsed.astimezone(_DISPLAY_TZ)
    pattern = "%d.%m.%Y · %H:%M" if with_year else "%d.%m · %H:%M"
    return parsed.strftime(pattern)


def event_value(event: dict[str, Any]) -> str:
    """Форматирует правую колонку события."""
    if event.get("type") == "worklog":
        return format_seconds(event.get("value"))
    return str(event.get("value") or event.get("type") or "—")


def task_order(issues: list[dict[str, Any]]) -> list[tuple[dict[str, Any], int]]:
    """Ставит дочерние задачи сразу после отображаемого родителя."""
    by_key = {issue.get("key"): issue for issue in issues if issue.get("key")}
    order = {
        issue.get("key"): index
        for index, issue in enumerate(issues)
        if issue.get("key")
    }
    children: dict[str, list[str]] = {}
    for issue in issues:
        key = issue.get("key")
        parent = issue.get("parent_key")
        if key and parent and parent in by_key:
            children.setdefault(parent, []).append(key)
    for key in children:
        children[key].sort(key=lambda child: order.get(child, 0))

    roots = [
        issue.get("key")
        for issue in issues
        if issue.get("key")
        and (
            not issue.get("parent_key")
            or issue.get("parent_key") not in by_key
        )
    ]
    roots.sort(key=lambda key: order.get(key, 0))
    result: list[tuple[dict[str, Any], int]] = []
    seen: set[str] = set()

    def append_branch(key: str, level: int) -> None:
        if key in seen:
            return
        seen.add(key)
        result.append((by_key[key], level))
        for child in children.get(key, []):
            append_branch(child, level + 1)

    for key in roots:
        append_branch(key, 0)
    for key in by_key:
        append_branch(key, 0)
    return result


def render_flow(stats: dict[str, Any]) -> str:
    """Рендерит текущий поток статусов."""
    counts = stats.get("status_counts") or {}
    parts = []
    for group in ("backlog", "progress", "paused", "review", "done"):
        label, color = STATUS_META[group]
        parts.append(
            '<div class="flow-step" style="--step-color:%s">'
            "<span>%s</span><strong>%d</strong><i></i></div>"
            % (color, esc(label), int(counts.get(group) or 0))
        )
    return "".join(parts)


def render_status_ledger(stats: dict[str, Any]) -> str:
    """Рендерит таблицу текущих статусов."""
    counts = stats.get("status_counts") or {}
    maximum = max([int(value or 0) for value in counts.values()] + [1])
    parts = []
    for group in ("backlog", "progress", "paused", "review", "done"):
        label, color = STATUS_META[group]
        count = int(counts.get(group) or 0)
        width = max(0, min(100, round(count / maximum * 100)))
        parts.append(
            '<div class="status-row" style="--dot:%s">'
            '<div class="status-name"><span class="status-dot"></span>%s</div>'
            '<div class="status-bar"><i style="--value:%d%%"></i></div>'
            '<div class="status-count">%d</div></div>'
            % (color, esc(label), width, count)
        )
    return "".join(parts)


def issue_link(key: Any, sibling_url: Any) -> str:
    """Кликабельный ключ задачи; база берётся из URL соседней задачи отчёта."""
    key = str(key or "").strip()
    if not key:
        return "Нет"
    base = str(sibling_url or "").split("/browse/")[0]
    if not base:
        return esc(key)
    return '<a href="%s/browse/%s">%s</a>' % (esc(base), esc(key), esc(key))


def render_issue_events(events: list[dict[str, Any]]) -> str:
    """Рендерит события внутри раскрытой задачи."""
    if not events:
        return '<p class="muted">За период событий пользователя не найдено.</p>'
    parts = ['<div class="mini-log">']
    for event in events:
        detail = event.get("detail") or "—"
        if event.get("possible_technical"):
            detail = "%s · возможный технический worklog" % detail
        parts.append(
            "<div><time>%s</time><span><b>%s</b> · %s</span><small>%s</small></div>"
            % (
                esc(format_datetime(event.get("timestamp"))),
                esc(event.get("label") or "Изменение"),
                esc(detail),
                esc(event_value(event)),
            )
        )
    parts.append("</div>")
    return "".join(parts)


def render_task(issue: dict[str, Any], level: int) -> str:
    """Рендерит одну раскрываемую строку реестра."""
    group = issue.get("status_group") or "backlog"
    assignee = issue.get("assignee") or {}
    original = int(issue.get("original_estimate_seconds") or 0)
    remaining = int(issue.get("remaining_estimate_seconds") or 0)
    spent = sum(
        int(item.get("seconds") or 0)
        for item in issue.get("worklogs") or []
    )
    # Значения фактов — уже готовый HTML: ключи задач кликабельны, остальное экранируется.
    facts = [
        ("Исполнитель", esc(assignee.get("name") or "Не назначен")),
        ("Создана", esc(format_datetime(issue.get("created"), with_year=True))),
        ("Обновлена", esc(format_datetime(issue.get("updated"), with_year=True))),
        (
            "Оценка",
            esc(
                "%s · осталось %s"
                % (format_seconds(original), format_seconds(remaining))
            ),
        ),
        (
            "Версия",
            esc(", ".join(issue.get("fix_versions") or []) or "Не указана"),
        ),
        ("Эпик", esc(issue.get("epic_key") or "Не указан")),
        ("Родитель", issue_link(issue.get("parent_key"), issue.get("url"))),
    ]
    fact_html = "".join(
        "<div><dt>%s</dt><dd>%s</dd></div>" % (esc(label), value)
        for label, value in facts
    )
    note_parts = []
    if issue.get("related_parent"):
        note_parts.append("связанный родитель, не включён в личную статистику")
    if issue.get("parent_key"):
        note_parts.append("дочерняя к %s" % issue.get("parent_key"))
    if not note_parts:
        note_parts.append(
            "%d %s пользователя за период"
            % (
                len(issue.get("events") or []),
                plural(
                    len(issue.get("events") or []),
                    "событие",
                    "события",
                    "событий",
                ),
            )
        )
    search_text = " ".join(
        str(value or "")
        for value in (
            issue.get("key"),
            issue.get("issue_type"),
            issue.get("summary"),
            issue.get("status"),
            assignee.get("name"),
        )
    ).casefold()
    priority = issue.get("priority") or "—"
    priority_html = (
        "<b>%s</b>" % esc(priority)
        if priority.casefold() in {"high", "highest", "высокий", "критический"}
        else esc(priority)
    )
    body_text = (
        "События ниже взяты из changelog, worklog и комментариев Jira. "
        "Пустые источники не заполняются предположениями."
    )
    return """
<details class="task-item" data-status="%s" data-level="%d" data-search="%s">
  <summary class="task-summary">
    <a class="task-key" href="%s">%s</a>
    <span class="task-type">%s</span>
    <span class="task-copy"><strong>%s</strong><small>%s</small></span>
    <span class="status-chip" style="%s">%s</span>
    <span class="priority">%s</span>
    <span class="time-cell"><strong>%s</strong><small>из %s</small></span>
  </summary>
  <div class="task-body">
    <dl class="task-facts">%s</dl>
    <div class="task-story">
      <h4>Что происходило</h4>
      <p>%s</p>
      %s
    </div>
  </div>
</details>""" % (
        esc(group),
        min(level, 1),
        esc(search_text),
        esc(issue.get("url") or "#"),
        esc(issue.get("key") or "—"),
        esc(issue.get("issue_type") or "—"),
        esc(issue.get("summary") or "Без описания"),
        esc(" · ".join(note_parts)),
        CHIP_STYLE.get(group, CHIP_STYLE["backlog"]),
        esc(issue.get("status") or "—"),
        priority_html,
        esc(format_seconds(spent)),
        esc(format_seconds(original)),
        fact_html,
        esc(body_text),
        render_issue_events(issue.get("events") or []),
    )


def render_tasks(issues: list[dict[str, Any]]) -> str:
    """Рендерит полный реестр без сокращения строк."""
    ordered = task_order(issues)
    if not ordered:
        return ""
    return "".join(render_task(issue, level) for issue, level in ordered)


def status_key(text: Any) -> str:
    """Сводит название статуса к группе отчёта.

    Changelog Jira отдаёт статусы по-английски («Ready for testing»), а поля
    задачи — по-русски, поэтому распознаются оба написания.
    """
    normalized = str(text or "").casefold()
    if any(
        word in normalized
        for word in ("выполн", "заверш", "done", "закры", "отмен", "cancel", "closed")
    ):
        return "done"
    if any(word in normalized for word in ("приостанов", "paused", "hold")):
        return "paused"
    if any(
        word in normalized
        for word in ("тестирован", "тест", "ревью", "review", "testing")
    ):
        return "review"
    if any(
        word in normalized
        for word in ("работ", "разработ", "progress", "in work", "development")
    ):
        return "progress"
    return "backlog"


def graph_nodes(issue: dict[str, Any]) -> list[dict[str, Any]]:
    """Возвращает узлы жизненного цикла: создание задачи и переходы статуса."""
    nodes = []
    for event in issue.get("events") or []:
        kind = event.get("type")
        if kind not in {"created", "transition"}:
            continue
        moment = parse_datetime(event.get("timestamp"))
        if not moment:
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=_DISPLAY_TZ or timezone.utc)
        if kind == "created":
            label, group = "Создана", "backlog"
        else:
            label = str(event.get("detail") or "").split("→")[-1].strip() or "—"
            group = status_key(label)
        nodes.append({"at": moment, "label": label, "group": group})
    nodes.sort(key=lambda node: node["at"])
    return nodes


def graph_grid(
    start: datetime,
    finish: datetime,
    position: Any,
    plot: float,
    height: int,
) -> list[str]:
    """Рисует часовую сетку: тонкая линия на каждый час, подпись — по шагу.

    Шаг подписей растёт, пока они не перестанут наезжать друг на друга, поэтому
    сетка одинаково читается и на дне, и на длинном периоде.
    """
    hours = (finish - start).total_seconds() / 3600 or 1 / 60
    per_hour = plot / hours
    step = next(
        (value for value in (1, 2, 3, 4, 6, 12, 24) if value * per_hour >= 46),
        max(24, int(hours / 8) or 24),
    )
    minor = 1 if per_hour >= 7 else step
    mark = start.replace(minute=0, second=0, microsecond=0)
    if mark < start:
        mark += timedelta(hours=1)
    parts: list[str] = []
    labelled = 0
    previous_day = None
    while mark <= finish:
        if mark.hour % minor == 0:
            tick_x = position(mark)
            is_label = mark.hour % step == 0
            parts.append(
                '<line class="%s" x1="%.1f" y1="%d" x2="%.1f" y2="%d"/>'
                % (
                    "tick" if is_label else "tick tick-minor",
                    tick_x,
                    GRAPH_TOP - 8,
                    tick_x,
                    height - 6,
                )
            )
            if is_label:
                labelled += 1
                same_day = previous_day == mark.date()
                previous_day = mark.date()
                parts.append(
                    '<text class="tick-label" x="%.1f" y="14" '
                    'text-anchor="middle">%s</text>'
                    % (
                        tick_x,
                        esc(
                            mark.strftime("%H:%M")
                            if same_day
                            else mark.strftime("%d.%m · %H:%M")
                        ),
                    )
                )
        mark += timedelta(hours=1)
    if labelled:
        return parts
    # Всё уместилось внутри одного часа — подписываем границы среза.
    return [
        '<text class="tick-label" x="%.1f" y="14" text-anchor="%s">%s</text>'
        % (position(moment), anchor, esc(moment.strftime("%d.%m · %H:%M")))
        for moment, anchor in ((start, "start"), (finish, "end"))
    ]


def render_graph(issues: list[dict[str, Any]]) -> str:
    """Рисует ветки жизненного цикла задач: узел на каждый переход, время по оси X."""
    lanes = [
        (issue, graph_nodes(issue))
        for issue in issues
        if not issue.get("related_parent")
    ]
    lanes = [lane for lane in lanes if lane[1]]
    if not lanes:
        return (
            '<p class="muted">За период не нашлось ни создания задачи, ни перехода '
            "статуса — рисовать нечего.</p>"
        )
    lanes.sort(key=lambda lane: lane[1][0]["at"])
    moments = [node["at"] for _, nodes in lanes for node in nodes]
    start, finish = min(moments), max(moments)
    span = (finish - start).total_seconds() or 1.0
    plot = GRAPH_WIDTH - GRAPH_LEFT - GRAPH_RIGHT
    height = GRAPH_TOP + len(lanes) * GRAPH_ROW + 10

    def position(moment: datetime) -> float:
        return GRAPH_LEFT + (moment - start).total_seconds() / span * plot

    parts = [
        '<svg class="graph" viewBox="0 0 %d %d" role="img" '
        'aria-label="Ход работы по задачам за период">'
        % (GRAPH_WIDTH, height)
    ]
    parts.extend(graph_grid(start, finish, position, plot, height))

    for row, (issue, nodes) in enumerate(lanes):
        y = GRAPH_TOP + row * GRAPH_ROW + GRAPH_ROW / 2
        parts.append(
            '<a href="%s"><text class="key" x="0" y="%.1f">%s</text></a>'
            % (esc(issue.get("url") or "#"), y + 4, esc(issue.get("key") or "—"))
        )
        for index, node in enumerate(nodes[:-1]):
            parts.append(
                '<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                'stroke-width="2.5" stroke-linecap="round"/>'
                % (
                    position(node["at"]),
                    y,
                    position(nodes[index + 1]["at"]),
                    y,
                    STATUS_META[node["group"]][1],
                )
            )
        last = nodes[-1]
        parts.append(
            '<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
            'stroke-width="2.5" stroke-dasharray="2 5" stroke-linecap="round" '
            'opacity=".55"/>'
            % (
                position(last["at"]),
                y,
                GRAPH_WIDTH - GRAPH_RIGHT,
                y,
                STATUS_META[last["group"]][1],
            )
        )
        for node in nodes:
            color = STATUS_META[node["group"]][1]
            parts.append(
                '<circle cx="%.1f" cy="%.1f" r="5" fill="%s" stroke="var(--paper)" '
                'stroke-width="2"><title>%s · %s</title></circle>'
                % (
                    position(node["at"]),
                    y,
                    color,
                    esc(node["at"].strftime("%d.%m · %H:%M")),
                    esc(node["label"]),
                )
            )
        # Changelog отдаёт статусы по-английски; если задача так и осталась в этом
        # состоянии, показываем её текущее русское название вместо «Ready for testing».
        current = str(issue.get("status") or "")
        end_label = current if status_key(current) == last["group"] else last["label"]
        parts.append(
            '<text class="end" x="%d" y="%.1f" fill="%s">%s</text>'
            % (
                GRAPH_WIDTH - GRAPH_RIGHT + 10,
                y + 4,
                STATUS_META[last["group"]][1],
                esc(end_label[:24]),
            )
        )
    parts.append("</svg>")

    legend = "".join(
        '<span style="--dot:%s"><i></i>%s</span>' % (color, esc(label))
        for label, color in (
            STATUS_META[group]
            for group in ("backlog", "progress", "paused", "review", "done")
        )
    )
    return (
        '<div class="graph-wrap">%s</div><div class="graph-legend">%s'
        "<span>Пунктир — состояние, в котором задача осталась на конец периода</span>"
        "</div>" % ("".join(parts), legend)
    )


def render_warnings(warnings: list[str]) -> tuple[str, str]:
    """Рендерит блок проверки и подпись навигации."""
    if not warnings:
        return (
            '<div class="audit-box"><h3>Предупреждений нет</h3>'
            "<ol><li>Все запрошенные источники Jira прочитаны.</li></ol></div>",
            "Без предупреждений",
        )
    items = "".join("<li>%s</li>" % esc(item) for item in warnings)
    count = len(warnings)
    verb = "требует" if count == 1 else "требуют"
    return (
        '<div class="audit-box"><h3>%d %s %s сверки</h3><ol>%s</ol></div>'
        % (
            count,
            plural(count, "место", "места", "мест"),
            verb,
            items,
        ),
        "%d %s %s сверки"
        % (count, plural(count, "место", "места", "мест"), verb),
    )


def render_sources(sources: list[dict[str, Any]]) -> str:
    """Рендерит аудит источников."""
    if not sources:
        return (
            '<div class="source-row"><strong>Источники</strong>'
            "<span>Метаданные источников отсутствуют.</span><b>неизвестно</b></div>"
        )
    return "".join(
        '<div class="source-row"><strong>%s</strong><span>%s</span><b>%s</b></div>'
        % (
            esc(source.get("name") or "Источник"),
            esc(source.get("detail") or "—"),
            esc(source.get("status") or "неизвестно"),
        )
        for source in sources
    )


def signed_number(value: int) -> str:
    """Форматирует изменение со знаком."""
    if value > 0:
        return "+%d" % value
    if value < 0:
        return "−%d" % abs(value)
    return "0"


def render_body(payload: dict[str, Any]) -> str:
    """Собирает содержимое отчёта внутри общего HTML-шаблона."""
    global _DISPLAY_TZ
    period = payload.get("period") or {}
    try:
        _DISPLAY_TZ = ZoneInfo(period.get("timezone") or "Europe/Moscow")
    except (ZoneInfoNotFoundError, ValueError):
        _DISPLAY_TZ = None
    scope = payload.get("scope") or {}
    stats = payload.get("stats") or {}
    issues = payload.get("issues") or []
    warnings = payload.get("warnings") or []
    report_user = scope.get("report_user") or {}
    status_counts = stats.get("status_counts") or {}
    audit_html, warning_label = render_warnings(warnings)
    issue_count = int(stats.get("issues") or 0)
    parent_count = int(stats.get("related_parents") or 0)
    assigned_count = int(stats.get("assigned_issues") or 0)
    completed = int(stats.get("completed") or 0)
    created = int(stats.get("created") or 0)
    task_rows = render_tasks(issues)
    registry_empty_class = (
        "registry-empty" if issues else "registry-empty is-visible"
    )
    graph_html = render_graph(issues)
    flow = render_flow(stats)
    status_ledger = render_status_ledger(stats)
    generated = format_datetime(payload.get("generated_at"), with_year=True)
    scope_note = scope.get("definition") or "Личная работа пользователя за период."
    net_change = created - completed
    possible_technical = int(
        stats.get("possible_technical_worklog_seconds") or 0
    )
    return """<div class="shell">
  <header class="hero">
    <div class="hero-nav">
      <div class="brand"><span class="brand-mark" aria-hidden="true"></span> %s / полный отчёт Jira</div>
      <div class="hero-actions">
        <button class="ghost-button" id="print-report" type="button">Печать</button>
        <button class="ghost-button" id="theme-toggle" type="button" aria-label="Переключить тему">Тема: авто</button>
      </div>
    </div>
    <div class="hero-grid">
      <div>
        <p class="hero-kicker">Работа в Jira · %s</p>
        <h1 class="hero-title">%s</h1>
        <div class="hero-meta">
          <span>Проект <b>%s</b></span>
          <span>Часовой пояс <b>%s</b></span>
          <span>Срез <b>%s</b></span>
        </div>
        <span class="demo-flag">%s</span>
      </div>
      <div class="scoreboard" aria-label="Итоги периода">
        <div class="score-line"><span>Задач в срезе</span><strong>%d</strong><small>%d назначены на пользователя, %d — связанные родители</small></div>
        <div class="score-line"><span>Завершено за период</span><strong>%d</strong><small>По changelog или resolutiondate; передача в тестирование засчитана</small></div>
        <div class="score-line"><span>Списано времени</span><strong>%s</strong><small>Только worklog пользователя с датой внутри периода</small></div>
        <div class="score-line"><span>Остаток оценки</span><strong>%s</strong><small>Текущий timeestimate незавершённых задач</small></div>
      </div>
    </div>
    <div class="flow" aria-label="Распределение задач по этапам">
      <div class="flow-head"><span>Поток работы на момент среза</span><span>%d %s · без двойного счёта родителей</span></div>
      <div class="flow-track">%s</div>
    </div>
  </header>

  <nav class="report-nav" aria-label="Разделы отчёта">
    <a href="#overview">Сводка</a>
    <a href="#graph">Ход работы</a>
    <a href="#tasks">Задачи</a>
    <a class="nav-alert" href="#checks">%s</a>
  </nav>

  <main class="main">
    <section class="section" id="overview">
      <div class="section-heading">
        <h2>Состояние работы</h2>
        <p>Измеримые состояния и движение за период. Связанные родители показаны в реестре, но не увеличивают личные показатели.</p>
      </div>
      <div class="overview-grid">
        <div class="status-ledger" aria-label="Задачи по статусам">%s</div>
        <aside class="period-balance">
          <div class="balance-row balance-emphasis"><span>Чистое изменение периода</span><strong>%s %s</strong><small>Создано %d, завершено %d</small></div>
          <div class="balance-row"><span>Задачи с движением</span><strong>%d из %d</strong><small>Создание, переход, worklog, комментарий или изменение поля</small></div>
          <div class="balance-row"><span>Без активности пользователя</span><strong>%d %s</strong><small>Задача вошла в JQL, но личных событий в периоде не найдено</small></div>
          <div class="balance-row"><span>Возможный технический worklog</span><strong>%s</strong><small>Эвристика: %s без комментария; не скрыт из итога</small></div>
        </aside>
      </div>
    </section>

    <section class="section" id="graph">
      <div class="section-heading">
        <h2>Ход работы</h2>
        <p>Ветка на задачу: узел — создание или переход статуса, цвет отрезка — состояние, в котором задача была до следующего узла. Наведи на узел, чтобы увидеть время и статус.</p>
      </div>
      %s
    </section>

    <section class="section" id="tasks">
      <div class="section-heading">
        <div><h2>Полный реестр задач</h2></div>
        <div class="section-actions"><button class="secondary-button" id="expand-tasks" type="button">Развернуть все</button></div>
      </div>
      <div class="controls">
        <label class="search-field">
          <input id="task-search" type="search" aria-label="Поиск по задачам" placeholder="Найти по ключу, описанию или типу" autocomplete="off">
        </label>
        <div class="filters" aria-label="Фильтр задач">
          <button class="filter-button" type="button" data-filter="all" aria-pressed="true">Все</button>
          <button class="filter-button" type="button" data-filter="backlog" aria-pressed="false">Очередь</button>
          <button class="filter-button" type="button" data-filter="progress" aria-pressed="false">В работе</button>
          <button class="filter-button" type="button" data-filter="paused" aria-pressed="false">Приостановлено</button>
          <button class="filter-button" type="button" data-filter="review" aria-pressed="false">Проверка</button>
          <button class="filter-button" type="button" data-filter="done" aria-pressed="false">Завершено</button>
        </div>
      </div>
      <div class="task-registry">
        <div class="registry-head" aria-hidden="true"><span>Ключ</span><span>Тип</span><span>Описание</span><span>Статус</span><span>Приоритет</span><span>Время</span></div>
        %s
        <div class="%s" id="registry-empty">По JQL не найдено задач или текущий фильтр ничего не показывает. Сбросьте фильтр либо проверьте границы периода.</div>
      </div>
      <div class="registry-note">
        <span id="task-count" aria-live="polite">Показано %d из %d строк</span>
        <span>Список не сокращён и не перегруппирован по выводам агента</span>
      </div>
    </section>

    <section class="section" id="checks">
      <div class="section-heading">
        <h2>Проверка данных</h2>
        <p>Неполные ответы Jira, лимиты и эвристики остаются частью отчёта и не исчезают молча.</p>
      </div>
      %s
      <div class="source-table" aria-label="Источники данных">%s</div>
    </section>
  </main>

  <footer class="footer">
    <span>Сформировано скиллом jira-browser-workflow</span>
    <span>JQL: %s</span>
  </footer>
</div>""" % (
        esc(scope.get("project") or "Jira"),
        esc(report_user.get("name") or report_user.get("login") or "пользователь"),
        esc(period.get("label") or "Период не указан"),
        esc(scope.get("project") or "—"),
        esc(period.get("timezone") or "—"),
        esc(generated),
        esc(scope_note),
        issue_count + parent_count,
        assigned_count,
        parent_count,
        completed,
        esc(format_seconds(stats.get("worklog_seconds"))),
        esc(format_seconds(stats.get("remaining_estimate_seconds"))),
        issue_count,
        plural(issue_count, "задача", "задачи", "задач"),
        flow,
        esc(warning_label),
        status_ledger,
        esc(signed_number(net_change)),
        plural(abs(net_change), "задача", "задачи", "задач"),
        created,
        completed,
        int(stats.get("active_issues") or 0),
        issue_count,
        int(stats.get("no_activity_issues") or 0),
        plural(
            int(stats.get("no_activity_issues") or 0),
            "задача",
            "задачи",
            "задач",
        ),
        esc(format_seconds(possible_technical)),
        esc("1m"),
        graph_html,
        task_rows,
        esc(registry_empty_class),
        len(issues),
        len(issues),
        audit_html,
        render_sources(payload.get("sources") or []),
        esc(scope.get("jql") or "не указан"),
    )


def load_payload(path: Path) -> dict[str, Any]:
    """Читает report-data из JSON или готового HTML."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.casefold() == ".html":
        match = re.search(
            r'<script type="application/json" id="report-data">(.*?)</script>',
            text,
            flags=re.DOTALL,
        )
        if not match:
            raise ValueError("В HTML нет блока report-data")
        text = match.group(1)
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("Корень report-data должен быть объектом")
    return payload


def render_html(payload: dict[str, Any], output: Path) -> None:
    """Записывает готовый отчёт одним HTML-файлом."""
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    body_start = template.index(BODY_MARKER)
    data_start = template.index(DATA_OPEN, body_start)
    data_end = template.index(DATA_CLOSE, data_start) + len(DATA_CLOSE)
    prefix = template[:body_start]
    suffix = template[data_end:]
    title = "Jira %s — %s" % (
        (payload.get("scope") or {}).get("project") or "",
        (payload.get("period") or {}).get("label") or "",
    )
    prefix = re.sub(
        r"<title>.*?</title>",
        "<title>%s</title>" % esc(title),
        prefix,
        count=1,
        flags=re.DOTALL,
    )
    embedded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("<", "\\u003c")
    page = (
        prefix
        + render_body(payload)
        + "\n\n"
        + DATA_OPEN
        + embedded
        + DATA_CLOSE
        + suffix
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding="utf-8")


def main() -> int:
    """Рендерит HTML из готового report-data."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        required=True,
        type=Path,
        help="Нормализованный JSON или HTML с блоком report-data",
    )
    parser.add_argument("--out", required=True, type=Path, help="Путь к HTML")
    args = parser.parse_args()
    try:
        payload = load_payload(args.json)
        render_html(payload, args.out)
    except (OSError, ValueError, KeyError) as error:
        print(
            json.dumps({"error": str(error)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1
    print(json.dumps({"ok": True, "html": str(args.out)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
