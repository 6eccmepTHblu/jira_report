#!/usr/bin/env python3
"""Клиент к корпоративной Jira: авторизация, кэш сессии и чтение через REST.

Выжимка из scripts/jira.py скилла jira-browser-workflow (MIT, Hermes Agent + Nous Research):
оставлено только то, что нужно отчётам — вход, сессия и чтение. Команды создания задач,
переходов по статусам и композитные сценарии сюда не переносились: приложение ничего
в Jira не меняет.

Аутентификация: form-login по JIRA_USERNAME / JIRA_PASSWORD из окружения (на Windows при
отсутствии в процессе читается User-окружение). Cookie-jar кэшируется в user-scoped файл
с правами 0600 и переиспользуется между запусками; при протухшей сессии клиент один раз
молча перелогинивается. Файл кэша — живой токен сессии, относись к нему как к секрету.
Пароль никогда не печатается и не логируется.
"""

import datetime
import json
import os
import re
import subprocess
import sys
from http.cookiejar import LWPCookieJar

import requests

# Инстанс-специфичная конфигурация. При смене настроек Jira меняй здесь.
CONFIG = {
    "base_url": "https://jira.dev.sgaz.pro",
    "project": "ATOM",
    "epic_link_field": "customfield_10000",  # Ссылка на эпик
}


def fail(obj, code=1):
    """Печатает JSON-ошибку в stderr и выходит."""
    print(json.dumps(obj, ensure_ascii=False, indent=2), file=sys.stderr)
    sys.exit(code)


class StepError(Exception):
    """Типизированная ошибка шага. Несёт JSON-payload для вывода.

    Для атомарных команд ловится в main() и печатается через fail() (exit 1).
    Для композитов run_steps() оборачивает её, добавляя прогресс до сбоя.
    """

    def __init__(self, payload):
        super().__init__(payload.get("error", "step_error"))
        self.payload = payload


# Правило Jira: голое число = минуты, h=часы, d=дни, w=недели.
_TIME_UNITS = {
    "w": "w", "wk": "w", "week": "w", "weeks": "w",
    "нед": "w", "неделя": "w", "недели": "w", "недель": "w",
    "d": "d", "day": "d", "days": "d",
    "дн": "d", "день": "d", "дня": "d", "дней": "d", "сутки": "d", "суток": "d",
    "h": "h", "hr": "h", "hrs": "h", "hour": "h", "hours": "h",
    "ч": "h", "час": "h", "часа": "h", "часов": "h",
    "m": "m", "min": "m", "mins": "m", "minute": "m", "minutes": "m",
    "м": "m", "мин": "m", "минута": "m", "минуты": "m", "минут": "m",
}


def normalize_time(value):
    """Приводит время к нотации Jira. Уже валидное ('3h', '1d 4h') не портит.

    None/пустое -> как есть. Нераспознанное (дробное, без числа, чужая единица) ->
    возвращаем исходную строку без изменений: пусть Jira сама валидирует.
    """
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s:
        return value
    if re.search(r"\d[.,]\d", s):  # дробное (1.5h) — не трогаем, Jira решит
        return value
    tokens = re.findall(r"(\d+)\s*([a-zа-я]*)", s)
    if not tokens:
        return value
    parts = []
    for num, unit in tokens:
        if unit == "":
            parts.append(num + "m")  # голое число -> минуты
            continue
        u = _TIME_UNITS.get(unit)
        if u is None:
            return value  # неизвестная единица -> отдать как есть
        parts.append(num + u)
    return " ".join(parts)


def _detail(r):
    """Тело ответа как JSON (если можно) или сырой текст."""
    try:
        return r.json()
    except ValueError:
        return r.text


def _read_user_env_windows(name):
    """Дочитывает переменную из User-окружения на Windows (обход устаревшего $env в процессе)."""
    if sys.platform != "win32":
        return None
    try:
        val = subprocess.check_output(
            ["powershell.exe", "-NoProfile", "-Command",
             "[Environment]::GetEnvironmentVariable('%s','User')" % name],
            text=True, timeout=15,
        ).strip()
        return val or None
    except Exception:
        return None


def _cred(name):
    return os.environ.get(name) or _read_user_env_windows(name)


def cache_path():
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "hermes", ".cache", "jira-session")
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "hermes", "jira-session")


def load_cookies(session):
    """Подгружает сохранённые cookie в сессию. True — если что-то загрузили."""
    path = cache_path()
    if not os.path.exists(path):
        return False
    jar = LWPCookieJar(path)
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception:
        return False
    session.cookies.update(jar)
    return len(session.cookies) > 0


def save_cookies(session):
    """Сохраняет cookie-jar в user-scoped файл с правами 0600. Кэш — оптимизация, ошибки глушим."""
    path = cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        jar = LWPCookieJar(path)
        for c in session.cookies:
            jar.set_cookie(c)
        jar.save(ignore_discard=True, ignore_expires=True)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        pass


def clear_cache():
    path = cache_path()
    existed = os.path.exists(path)
    if existed:
        os.remove(path)
    return existed, path


def _authed(r):
    """Аутентифицирован ли ответ. Jira отдаёт X-AUSERNAME: <user> | anonymous."""
    who = r.headers.get("X-AUSERNAME")
    if who is not None:
        return who.strip().lower() != "anonymous"
    return r.status_code != 401


def form_login(session):
    """Выполняет form-login в переданную сессию (на месте). Пароль не логируется."""
    base = CONFIG["base_url"]
    username = _cred("JIRA_USERNAME")
    password = _cred("JIRA_PASSWORD")
    if not username or not password:
        fail({"error": "credentials_missing",
              "hint": "Установи JIRA_USERNAME и JIRA_PASSWORD в окружении."})
    try:
        session.get(base + "/login.jsp", timeout=20)
        session.post(base + "/login.jsp",
                     data={"os_username": username, "os_password": password,
                           "os_cookie": "true", "login": "Вход"},
                     timeout=20)
        me = session.get(base + "/rest/api/2/myself", timeout=15)
    except requests.RequestException as e:
        fail({"error": "network_error", "detail": str(e)})
    if me.status_code != 200 or not _authed(me):
        fail({"error": "auth_failed", "status": me.status_code,
              "hint": "form-login не прошёл (возможен SSO/2FA) — используй браузерный фолбэк."})


def get_session(fresh=False):
    """Готовит сессию: из кэша (с ленивым авто-релогином) или свежим логином."""
    s = requests.Session()
    if fresh:
        form_login(s)
        save_cookies(s)
        return s
    if not load_cookies(s):
        # Кэша нет — логинимся сразу, чтобы не ловить гарантированный 401 на первом запросе.
        form_login(s)
        save_cookies(s)
    return s


def api(session, method, path, _retry=True, check=True, **kwargs):
    """REST с CSRF-заголовком на запись и одним авто-релогином при протухшей сессии.

    check=True (по умолчанию): на HTTP >= 400 сразу fail() и выход — поведение,
    на которое рассчитаны атомарные команды. check=False: вернуть ответ как есть
    (даже 4xx), чтобы вызывающий сам разобрал ошибку (типизация required-полей,
    авто-retry). Авто-релогин при 401/anonymous работает независимо от check.
    """
    url = path if path.startswith("http") else CONFIG["base_url"] + path
    if method.upper() in ("POST", "PUT", "DELETE"):
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("X-Atlassian-Token", "no-check")
        kwargs["headers"] = headers
    try:
        r = session.request(method, url, timeout=kwargs.pop("timeout", 30), **kwargs)
    except requests.RequestException as e:
        # StepError (не sys.exit): внутри композита run_steps добавит failed_step/completed/
        # recovery, для атомарных команд main() поймает и напечатает через fail().
        raise StepError({"error": "network_error", "method": method, "path": path,
                         "detail": str(e)})
    if _retry and (r.status_code == 401 or
                   r.headers.get("X-AUSERNAME", "").strip().lower() == "anonymous"):
        # Кэш протух — перелогинимся и повторим запрос ровно один раз.
        form_login(session)
        save_cookies(session)
        return api(session, method, path, _retry=False, check=check, **kwargs)
    if check and r.status_code >= 400:
        raise StepError({"error": "http_%d" % r.status_code, "method": method, "path": path,
                         "detail": _detail(r)})
    return r


def issue_url(key):
    return "%s/browse/%s" % (CONFIG["base_url"], key)


def jira_time_seconds(value):
    """Грубо переводит нормализованное время Jira в секунды для сравнения worklog."""
    value = normalize_time(value)
    if not value:
        return None
    total = 0
    matched = False
    for num, unit in re.findall(r"(\d+)\s*([wdhm])", str(value).lower()):
        matched = True
        n = int(num)
        if unit == "w":
            total += n * 5 * 8 * 3600
        elif unit == "d":
            total += n * 8 * 3600
        elif unit == "h":
            total += n * 3600
        elif unit == "m":
            total += n * 60
    return total if matched else None


def list_worklogs(session, key):
    """Читает worklog задачи с пагинацией."""
    start = 0
    items = []
    while True:
        r = api(session, "GET", "/rest/api/2/issue/%s/worklog" % key,
                params={"startAt": start, "maxResults": 100})
        data = r.json()
        chunk = data.get("worklogs") or []
        items.extend(chunk)
        start += len(chunk)
        if start >= data.get("total", len(items)) or not chunk:
            return items
