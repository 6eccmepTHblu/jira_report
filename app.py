#!/usr/bin/env python3
"""Локальное веб-приложение: выгрузка активности из Jira за период → HTML + бланк xlsx.

Сбор данных и вёрстку HTML делает скилл jira-browser-workflow (scripts/generate_report.py),
здесь только интерфейс, вход и выдача готовых файлов.

Логин и пароль вводятся на странице и живут только в памяти процесса: на диск приложение
их не пишет. После входа скилл кэширует cookie-сессию Jira, поэтому пароль не нужен до
следующего «Выйти». Если JIRA_USERNAME / JIRA_PASSWORD заданы в окружении, вход не нужен.

    python app.py            # откроет http://127.0.0.1:8765
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import threading
import webbrowser
from datetime import date, datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

BASE_DIR = Path(__file__).parent
REPORTS_DIR = BASE_DIR / "reports"
PORT = int(os.environ.get("JIRA_REPORT_PORT", "8765"))

SKILL_DIR = Path(os.environ.get("JIRA_SKILL_DIR") or (
    Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "skills" / "productivity"
    / "jira-browser-workflow"))
sys.path.insert(0, str(SKILL_DIR / "scripts"))

try:
    import generate_report
    import jira
    from render_html import render_html
except ImportError as error:  # скилл не на месте — единственная внешняя зависимость
    raise SystemExit(
        "Не найден скилл jira-browser-workflow в %s (%s).\n"
        "Укажи путь переменной JIRA_SKILL_DIR." % (SKILL_DIR, error))

import xlsx_report

# «Выйти» должен работать и когда логин лежит в переменных окружения Windows: пока флаг
# поднят, приложение не пробует авторизоваться само и показывает форму входа.
STATE = {"force_login": False}


# --- Jira ----------------------------------------------------------------------
def jira_identity() -> tuple[bool, str]:
    """Кто вошёл. Возвращает (ок, имя пользователя либо причину отказа)."""
    if STATE["force_login"]:
        return False, "Войдите под своей учётной записью Jira"
    try:
        session = jira.get_session()
        me = jira.api(session, "GET", "/rest/api/2/myself").json()
        return True, "%s · %s" % (me.get("displayName"), me.get("name"))
    except SystemExit:
        # jira.fail() завершает процесс — в вебе это недопустимо, ловим и показываем.
        return False, "Логин и пароль не заданы"
    except Exception as error:
        return False, str(error)


def jira_login(username: str, password: str) -> tuple[bool, str]:
    """Проверяет пару логин/пароль и запоминает её в окружении процесса.

    Пароль остаётся только в памяти: jira.py читает его из окружения, а cookie-сессию
    кэширует сам. На диск приложение пароль не пишет.
    """
    previous = {name: os.environ.get(name) for name in ("JIRA_USERNAME", "JIRA_PASSWORD")}
    os.environ["JIRA_USERNAME"] = username
    os.environ["JIRA_PASSWORD"] = password
    try:
        # fresh=True логинится заново и перезаписывает кэш только при успехе, поэтому
        # неудачная попытка не выбрасывает уже работающую сессию.
        session = jira.get_session(fresh=True)
        me = jira.api(session, "GET", "/rest/api/2/myself").json()
    except SystemExit:
        _restore_env(previous)
        return False, "Jira отклонила логин или пароль"
    except Exception as error:
        _restore_env(previous)
        return False, str(error)
    STATE["force_login"] = False
    return True, me.get("displayName") or username


def _restore_env(previous: dict[str, str | None]) -> None:
    for name, value in previous.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def jira_logout() -> None:
    """Забывает пароль и кэш сессии, возвращает к форме входа."""
    for name in ("JIRA_USERNAME", "JIRA_PASSWORD"):
        os.environ.pop(name, None)
    try:
        jira.clear_cache()
    except OSError:
        pass
    STATE["force_login"] = True


# --- Данные --------------------------------------------------------------------
PERIOD_IN_NAME = re.compile(r"(\d{4}-\d{2}-\d{2})(?:_(\d{4}-\d{2}-\d{2}))?")


def describe_report(path: Path) -> dict:
    """Человекочитаемое описание файла из reports/."""
    match = PERIOD_IN_NAME.search(path.name)
    period = ""
    if match:
        since = date.fromisoformat(match.group(1))
        until = date.fromisoformat(match.group(2)) if match.group(2) else since
        period = ("%s" % since.strftime("%d.%m.%Y") if until == since
                  else "%s–%s" % (since.strftime("%d.%m"), until.strftime("%d.%m.%Y")))
    kind = "Бланк отчёта" if path.name.startswith("Отчет_о") else "Отчёт по задачам"
    stat = path.stat()
    return {"name": path.name, "kind": kind, "period": period,
            "format": path.suffix.lstrip(".").upper(),
            "when": datetime.fromtimestamp(stat.st_mtime).strftime("%d.%m.%Y, %H:%M"),
            "size": "%.0f КБ" % (stat.st_size / 1024)}


def existing_reports() -> list[dict]:
    """Готовые отчёты в reports/, новые сверху."""
    files = [p for p in REPORTS_DIR.glob("*") if p.suffix in (".html", ".xlsx")]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [describe_report(p) for p in files]


def group_warnings(warnings: list[str]) -> dict[str, list[str]]:
    """Схлопывает однотипные предупреждения: 13 строк про changelog → одна со списком задач."""
    grouped: dict[str, list[str]] = {}
    for warning in warnings:
        key, _, text = warning.partition(": ")
        if text and key.replace("-", "").isalnum():
            grouped.setdefault(text, []).append(key)
        else:
            grouped.setdefault(warning, [])
    return grouped


def personal_jql(login: str, since: date, until: date) -> str:
    """Стандартная выборка генератора плюс действия пользователя по чужим задачам.

    Генератор ищет задачи, где пользователь создатель, исполнитель или автор worklog.
    Этого мало: перевод чужой задачи в тестирование с переназначением на тестировщика
    не оставляет ни одного из трёх следов, и работа пропадает из отчёта. Добавляем
    прямые сигналы действия — смену статуса и смену исполнителя.
    """
    quote = generate_report.jql_quote
    hour = generate_report.DAY_START_HOUR
    start = "%s %02d:00" % (since.isoformat(), hour)
    end = "%s %02d:00" % ((until + timedelta(days=1)).isoformat(), hour)
    changed = " OR ".join(
        "(%s CHANGED BY %s DURING (%s, %s))" % (field, quote(login), quote(start), quote(end))
        for field in ("status", "assignee"))
    base = generate_report.build_default_jql(login, since, until)
    marker = ") ORDER BY"
    if marker not in base:  # генератор сменил формат — работаем на его выборке
        return base
    return base.replace(marker, " OR %s%s" % (changed, marker), 1)


def build(since: date, until: date, work_start: time, employee: str | None,
          include_parents: bool, timezone_name: str = "Europe/Moscow") -> dict:
    """Выгружает период и складывает HTML, JSON и заполненный бланк в reports/."""
    session = jira.get_session()
    login = jira.api(session, "GET", "/rest/api/2/myself").json().get("name")
    collect = lambda jql: generate_report.collect_report(
        session, since, until, timezone_name=timezone_name,
        assignee=None, jql=jql, max_results=200, include_parents=include_parents)
    fallback = None
    try:
        payload = collect(personal_jql(login, since, until) if login else None)
    except jira.StepError:
        # `CHANGED BY` поддерживают не все версии Jira: откатываемся на штатную выборку,
        # но честно говорим, что действия по чужим задачам могли не попасть.
        payload = collect(None)
        fallback = ("Jira отвергла запрос со сменой статуса и исполнителя; использована "
                    "стандартная выборка. Действия по чужим задачам могли не попасть в отчёт.")
    if fallback:
        payload["warnings"].append(fallback)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    html_path, json_path = generate_report.output_paths(REPORTS_DIR, since, until)
    render_html(payload, html_path)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    suffix = since.isoformat() + ("" if until == since else "_%s" % until.isoformat())
    xlsx_path = REPORTS_DIR / ("Отчет_о_проделанной_работе_%s.xlsx" % suffix)
    blank = xlsx_report.build_xlsx(payload, xlsx_path, work_start=work_start, employee=employee)

    # Задачи с движением, но без списаний, в бланк не попадают — показываем их отдельно,
    # чтобы работа не потерялась молча.
    login = (payload.get("scope", {}).get("report_user") or {}).get("login")
    skipped = [i for i in payload["issues"]
               if not any(w.get("author_login") == login for w in i.get("worklogs", []))]
    return {"payload": payload, "html": html_path, "xlsx": xlsx_path,
            "blank": blank, "skipped": skipped, "period": (since, until)}


# --- Разметка ------------------------------------------------------------------
STYLE = """
:root {
  color-scheme: light dark;
  --bg: #f4f4f5;  --surface: #fff;      --sunken: #fafafa;
  --ink: #17171b;  --muted: #62626d;    --line: #e2e2e6;  --line-strong: #cfcfd6;
  --accent: #2f5fd0; --accent-ink: #fff; --accent-soft: #eef2fd;
  --ok: #1f7a45;   --bad: #b3261e;      --bad-soft: #fdf0ef;
  --radius: 10px;
  --shadow: 0 1px 2px rgb(20 20 30 / .05), 0 12px 28px -18px rgb(20 20 30 / .35);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f0f12;  --surface: #17171b;  --sunken: #131317;
    --ink: #ececf1; --muted: #9d9daa;    --line: #2a2a31;  --line-strong: #3a3a44;
    --accent: #7ba2f5; --accent-ink: #10131c; --accent-soft: #1b2233;
    --ok: #5fd08c;  --bad: #f2837a;      --bad-soft: #2a1a19;
    --shadow: 0 1px 2px rgb(0 0 0 / .4), 0 12px 28px -18px rgb(0 0 0 / .8);
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.55 system-ui, "Segoe UI", Roboto, sans-serif;
  -webkit-font-smoothing: antialiased;
}
main { max-width: 54rem; margin: 0 auto; padding: 2.5rem 1.5rem 5rem; }
h1 { font-size: 1.375rem; font-weight: 600; letter-spacing: -.01em; margin: 0; }
h2 { font-size: inherit; font-weight: 600; margin: 2.75rem 0 .85rem; }
p { margin: 0; }
a { color: var(--accent); text-decoration-color: color-mix(in srgb, var(--accent) 35%, transparent);
    text-underline-offset: 2px; }
a:hover { text-decoration-color: currentColor; }

header {
  display: flex; flex-wrap: wrap; gap: .75rem 1.25rem;
  align-items: baseline; justify-content: space-between; margin-bottom: 2rem;
}
.session { display: flex; align-items: center; gap: .5rem; color: var(--muted); }
.dot { width: .5rem; height: .5rem; border-radius: 50%; background: var(--ok); flex: none; }
.dot.off { background: var(--line-strong); }
.linkish {
  background: none; border: 0; padding: 0; font: inherit; color: var(--muted);
  cursor: pointer; text-decoration: underline; text-underline-offset: 2px;
}
.linkish:hover { color: var(--ink); }

.panel {
  background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--radius); box-shadow: var(--shadow); padding: 1.5rem;
}
.panel + .panel { margin-top: 1rem; }
.lede { color: var(--muted); margin-bottom: 1.25rem; }

.fields { display: grid; gap: 1.1rem 1rem; grid-template-columns: repeat(auto-fit, minmax(9.5rem, 1fr)); }
.full { grid-column: 1 / -1; }
label { display: block; font-size: .75rem; font-weight: 500; color: var(--muted); margin-bottom: .35rem; }
input[type=date], input[type=time], input[type=text], input[type=password] {
  width: 100%; padding: .55rem .7rem; font: inherit; color: var(--ink);
  background: var(--sunken); border: 1px solid var(--line-strong); border-radius: 8px;
  transition: border-color .15s, box-shadow .15s;
}
input::placeholder { color: color-mix(in srgb, var(--muted) 80%, transparent); }
input:hover { border-color: color-mix(in srgb, var(--accent) 40%, var(--line-strong)); }
input:focus-visible, button:focus-visible, .seg button:focus-visible {
  outline: 2px solid var(--accent); outline-offset: 2px;
}
input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); outline: none; }

.seg { display: flex; flex-wrap: wrap; gap: .35rem; }
.seg button {
  padding: .45rem .75rem; font: inherit; color: var(--muted);
  background: var(--sunken); border: 1px solid var(--line-strong); border-radius: 7px;
  cursor: pointer; transition: color .15s, border-color .15s, background .15s;
}
.seg button:hover { color: var(--ink); border-color: var(--accent); }
.seg button[aria-pressed=true] {
  color: var(--accent); border-color: var(--accent); background: var(--accent-soft);
}

.actions { display: flex; flex-wrap: wrap; align-items: center; gap: 1rem; margin-top: 1.5rem; }
button.primary {
  padding: .6rem 1.3rem; font: inherit; font-weight: 500; color: var(--accent-ink);
  background: var(--accent); border: 0; border-radius: 8px; cursor: pointer;
  transition: filter .15s;
}
button.primary:hover { filter: brightness(1.08); }
button.primary:disabled { cursor: progress; filter: saturate(.35) brightness(1.1); }
.check { display: flex; align-items: center; gap: .5rem; color: var(--muted); }
.check input { accent-color: var(--accent); width: 1rem; height: 1rem; }
.check label { margin: 0; font-size: inherit; }

progress { display: none; width: 100%; height: 3px; margin-top: 1.25rem;
           border: 0; border-radius: 2px; background: var(--line); accent-color: var(--accent); }
form.busy progress { display: block; }
form.busy .fields, form.busy .check { opacity: .5; }
.waiting { display: none; color: var(--muted); }
form.busy .waiting { display: block; }

.summary { line-height: 1.6; }
.summary b { font-weight: 600; font-variant-numeric: tabular-nums; }
.summary .quiet { color: var(--muted); }
.downloads { display: flex; flex-wrap: wrap; gap: .6rem; margin-top: 1.35rem; }
.downloads a {
  padding: .55rem 1rem; border: 1px solid var(--line-strong); border-radius: 8px;
  text-decoration: none; color: var(--ink);
  transition: border-color .15s, background .15s;
}
.downloads a:hover { border-color: var(--accent); background: var(--accent-soft); }
.downloads a.lead { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
.downloads a.lead:hover { filter: brightness(1.08); }

table { width: 100%; border-collapse: collapse; }
th { text-align: left; font-weight: 500; font-size: .75rem; color: var(--muted);
     padding: 0 .75rem .5rem 0; border-bottom: 1px solid var(--line); }
td { padding: .6rem .75rem .6rem 0; border-bottom: 1px solid var(--line); vertical-align: top; }
tr:last-child td { border-bottom: 0; }
td.num, th.num { text-align: right; padding-right: 0; font-variant-numeric: tabular-nums;
                 color: var(--muted); white-space: nowrap; }
.tag { display: inline-block; padding: .1rem .4rem; border: 1px solid var(--line-strong);
       border-radius: 5px; font-size: .75rem; letter-spacing: .02em; color: var(--muted); }

.notice { padding: .85rem 1rem; border: 1px solid var(--line); border-radius: 8px;
          background: var(--sunken); color: var(--muted); }
.notice.error { border-color: color-mix(in srgb, var(--bad) 45%, var(--line));
                background: var(--bad-soft); color: var(--bad); }
.notice + table { margin-top: 1rem; }
details { margin-top: 1.5rem; color: var(--muted); }
summary { cursor: pointer; color: var(--muted); }
summary:hover { color: var(--ink); }
details ul { margin: .75rem 0 0; padding-left: 1.1rem; }
details li { margin-bottom: .4rem; }
.empty { color: var(--muted); }
/* На узком экране размер файла — наименее нужная колонка, уступает место названию. */
@media (max-width: 34rem) {
  main { padding: 1.75rem 1rem 3rem; }
  .panel { padding: 1.15rem; }
  .files th:last-child, .files td:last-child { display: none; }
  .downloads a { flex: 1 1 100%; text-align: center; }
}
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
"""

SCRIPT = """
document.querySelectorAll('.seg button').forEach(node => node.onclick = () => {
  const iso = d => new Date(d.getTime() - d.getTimezoneOffset() * 6e4).toISOString().slice(0, 10);
  const back = n => { const d = new Date(); d.setDate(d.getDate() - n); return d; };
  const weekday = (new Date().getDay() + 6) % 7;
  const range = {
    today: [back(0), back(0)],
    yesterday: [back(1), back(1)],
    week: [back(weekday), back(0)],
    prev: [back(weekday + 7), back(weekday + 1)],
  }[node.dataset.preset];
  document.querySelector('#since').value = iso(range[0]);
  document.querySelector('#until').value = iso(range[1]);
  document.querySelectorAll('.seg button').forEach(b => b.setAttribute('aria-pressed', b === node));
});
const form = document.querySelector('#build-form');
if (form) form.addEventListener('submit', () => {
  form.classList.add('busy');
  form.querySelector('button.primary').disabled = true;
  form.querySelector('button.primary').textContent = 'Собираю…';
});
"""

PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>{style}</style></head>
<body><main>
<header>
  <h1>Отчёт по работе в Jira</h1>
  {session}
</header>
{body}
</main><script>{script}</script></body></html>"""


def session_html(ok: bool, who: str) -> str:
    if not ok:
        return '<p class="session"><span class="dot off"></span>Не подключено</p>'
    return ('<p class="session"><span class="dot"></span>%s'
            '<form method="post" action="/logout" style="display:inline">'
            '<button class="linkish" type="submit">Выйти</button></form></p>'
            % html.escape(who))


def login_html(error: str | None = None) -> str:
    return """
<div class="panel">
  <h2 style="margin-top:0">Вход в Jira</h2>
  <p class="lede">Учётная запись jira.dev.sgaz.pro, та же, с которой вы открываете задачи.
     Пароль остаётся в памяти приложения и на диск не записывается.</p>
  %s
  <form method="post" action="/login" autocomplete="on">
    <div class="fields">
      <div class="full"><label for="username">Логин</label>
        <input type="text" id="username" name="username" autocomplete="username"
               placeholder="например, ulanovvs" required autofocus></div>
      <div class="full"><label for="password">Пароль</label>
        <input type="password" id="password" name="password"
               autocomplete="current-password" required></div>
    </div>
    <div class="actions"><button class="primary" type="submit">Войти</button></div>
  </form>
</div>""" % ('<p class="notice error" style="margin-bottom:1.25rem">%s</p>' % html.escape(error)
             if error else "")


def form_html(since: date, until: date) -> str:
    return """
<div class="panel">
  <h2 style="margin-top:0">Новый отчёт</h2>
  <p class="lede">Соберу все ваши действия за период: списанное время, переходы задач,
     комментарии. На выходе — читаемый отчёт и заполненный бланк.</p>
  <form method="post" action="/build" id="build-form">
    <div class="fields">
      <div class="full"><label>Период</label>
        <div class="seg">
          <button type="button" data-preset="today" aria-pressed="false">Сегодня</button>
          <button type="button" data-preset="yesterday" aria-pressed="false">Вчера</button>
          <button type="button" data-preset="week" aria-pressed="true">Эта неделя</button>
          <button type="button" data-preset="prev" aria-pressed="false">Прошлая неделя</button>
        </div></div>
      <div><label for="since">С</label>
        <input type="date" id="since" name="since" value="%s" required></div>
      <div><label for="until">По</label>
        <input type="date" id="until" name="until" value="%s" required></div>
      <div><label for="work_start">Начало рабочего дня</label>
        <input type="time" id="work_start" name="work_start" value="09:00" required></div>
      <div><label for="employee">ФИО в бланке</label>
        <input type="text" id="employee" name="employee" placeholder="как в Jira"></div>
    </div>
    <div class="actions">
      <button class="primary" type="submit">Собрать отчёт</button>
      <span class="check"><input type="checkbox" id="parents" name="include_parents">
        <label for="parents">включить родительские задачи</label></span>
      <span class="waiting">Читаю задачи, worklog и историю: обычно 20–40 секунд.</span>
    </div>
    <progress></progress>
  </form>
</div>""" % (since.isoformat(), until.isoformat())


def reports_html() -> str:
    reports = existing_reports()
    if not reports:
        return ('<h2>Готовые файлы</h2>'
                '<p class="empty">Пока пусто. Соберите первый отчёт — файлы появятся здесь '
                'и останутся доступны после перезапуска.</p>')
    rows = "".join(
        '<tr><td><a href="/reports/%s">%s</a>%s</td><td class="num"><span class="tag">%s</span></td>'
        '<td class="num">%s</td><td class="num">%s</td></tr>'
        % (quote(r["name"]), html.escape(r["kind"]),
           '<br><span class="empty">%s</span>' % r["period"] if r["period"] else "",
           r["format"], r["when"], r["size"])
        for r in reports)
    return ('<h2>Готовые файлы</h2><table class="files"><tr><th>Отчёт</th><th class="num">Формат</th>'
            '<th class="num">Собран</th><th class="num">Размер</th></tr>%s</table>' % rows)


def result_html(result: dict) -> str:
    stats = result["payload"]["stats"]
    since, until = result["period"]
    when = (since.strftime("%d.%m.%Y") if since == until
            else "%s–%s" % (since.strftime("%d.%m"), until.strftime("%d.%m.%Y")))
    parts = ['<div class="panel"><p class="summary">За <b>%s</b>: <b>%d</b> задач, '
             'из них <b>%d</b> завершено, списано <b>%.1f ч</b>. '
             '<span class="quiet">В бланк легло %d строк за %d дней, '
             'на листе «Все действия» — %d записей.</span></p>'
             % (when, stats["issues"], stats["completed"], stats["worklog_seconds"] / 3600,
                result["blank"]["rows"], result["blank"]["days"],
                result["blank"]["activity"])]
    parts.append('<div class="downloads">'
                 '<a class="lead" href="/reports/%s">Скачать бланк xlsx</a>'
                 '<a href="/reports/%s" target="_blank">Открыть отчёт по задачам</a></div>'
                 % (quote(result["xlsx"].name), quote(result["html"].name)))

    if result["skipped"]:
        rows = "".join('<tr><td><a href="%s" target="_blank">%s</a></td><td>%s</td>'
                       '<td class="num">%s</td></tr>'
                       % (i["url"], i["key"], html.escape(i["summary"]), html.escape(i["status"]))
                       for i in result["skipped"])
        parts.append('<h2>Не попало в бланк</h2>'
                     '<p class="notice">У этих задач за период нет списанного времени, '
                     'а строки без трудозатрат бланк не предусматривает. '
                     'Впишите вручную, если работа была.</p>'
                     '<table><tr><th>Задача</th><th>Тема</th><th class="num">Статус</th></tr>'
                     '%s</table>' % rows)

    grouped = group_warnings(result["payload"]["warnings"])
    if grouped:
        items = "".join(
            '<li>%s%s</li>' % (html.escape("%s: " % ", ".join(keys)) if keys else "",
                               html.escape(text))
            for text, keys in grouped.items())
        parts.append('<details><summary>Замечания сборщика данных (%d)</summary>'
                     '<ul>%s</ul></details>' % (len(grouped), items))
    parts.append("</div>")
    return "".join(parts)


# --- Сервер --------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "JiraReport"

    def log_message(self, *args):  # тише в консоли; и пароль не должен попасть в лог
        pass

    def _send(self, body: bytes, content_type: str, status: int = 200,
              filename: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if filename:
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''%s" % quote(filename))
        self.end_headers()
        self.wfile.write(body)

    def _page(self, body: str, status: int = 200, login_error: str | None = None) -> None:
        ok, who = jira_identity()
        content = body if ok else login_html(login_error or (None if STATE["force_login"] else who))
        self._send(PAGE.format(title="Отчёт по работе в Jira", style=STYLE, script=SCRIPT,
                               session=session_html(ok, who), body=content).encode("utf-8"),
                   "text/html; charset=utf-8", status)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _home(self, extra: str = "", status: int = 200, login_error: str | None = None) -> None:
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        self._page(extra + form_html(monday, today) + reports_html(), status, login_error)

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path == "/":
            self._home()
            return
        if path.startswith("/reports/"):
            target = REPORTS_DIR / Path(path[len("/reports/"):]).name
            if not target.is_file():
                self._home('<p class="notice error">Файл не найден.</p>', 404)
                return
            if target.suffix == ".html":
                self._send(target.read_bytes(), "text/html; charset=utf-8")
            else:
                self._send(target.read_bytes(),
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           filename=target.name)
            return
        self._home('<p class="notice error">Страница не найдена.</p>', 404)

    def _form(self) -> dict[str, list[str]]:
        length = int(self.headers.get("Content-Length", 0))
        return parse_qs(self.rfile.read(length).decode("utf-8"))

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        form = self._form()
        value = lambda name, default="": (form.get(name) or [default])[0].strip()

        if path == "/login":
            ok, message = jira_login(value("username"), value("password"))
            if not ok:
                self._page("", 401, login_error=message)
                return
            self._redirect("/")
            return

        if path == "/logout":
            jira_logout()
            self._redirect("/")
            return

        if path != "/build":
            self._home('<p class="notice error">Страница не найдена.</p>', 404)
            return

        try:
            since = date.fromisoformat(value("since"))
            until = date.fromisoformat(value("until"))
            if until < since:
                raise ValueError("Конец периода раньше начала — поменяйте даты местами.")
            hour, minute = (int(part) for part in value("work_start", "09:00").split(":"))
            # ponytail: сбор идёт синхронно, страница ждёт (~30 c на неделю).
            # Если начнёт мешать — вынести в поток и опрашивать статус.
            result = build(since, until, time(hour, minute), value("employee") or None,
                           bool(form.get("include_parents")))
        except SystemExit:
            STATE["force_login"] = True
            self._page("", 401, login_error="Jira отклонила сессию — войдите заново.")
            return
        except (ValueError, jira.StepError) as error:
            detail = error.payload if isinstance(error, jira.StepError) else str(error)
            self._home('<p class="notice error">%s</p>' % html.escape(str(detail)), 400)
            return

        self._page(result_html(result) + reports_html())


def main() -> int:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    # Windows резервирует куски диапазона под Hyper-V/WSL: занятый порт даёт WinError 10013.
    for port in range(PORT, PORT + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    else:
        raise SystemExit("Не удалось занять порт в диапазоне %d–%d." % (PORT, PORT + 19))
    url = "http://127.0.0.1:%d/" % port
    print("Отчёты по Jira: %s  (Ctrl+C — остановить)" % url)
    threading.Timer(0.5, webbrowser.open, args=[url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
