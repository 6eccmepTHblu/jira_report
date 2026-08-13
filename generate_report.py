#!/usr/bin/env python3
"""Собирает полный отчёт по работе пользователя в Jira и записывает HTML.

Отчёт строится за обязательный день или период. В выборку входят задачи проекта,
где пользователь является текущим/бывшим исполнителем, создателем или автором
worklog. Для найденных задач отдельно читаются worklog, комментарии и changelog.
Недоступность вторичного источника не скрывается: задача остаётся в отчёте, а
проблема попадает в warnings.

Перенесено из скилла jira-browser-workflow (MIT, Hermes Agent + Nous Research), чтобы приложение работало самостоятельно.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import jira_client as jira
from render_html import render_html


REPORT_FIELDS = ",".join(
    [
        "summary",
        "status",
        "issuetype",
        "priority",
        "assignee",
        "reporter",
        "creator",
        "created",
        "updated",
        "resolution",
        "resolutiondate",
        "timetracking",
        "timeoriginalestimate",
        "timeestimate",
        "timespent",
        "fixVersions",
        "issuelinks",
        jira.CONFIG["epic_link_field"],
    ]
)
LINK_TYPE = "Parent-Child"
# Отчётные сутки начинаются в 05:00 локального времени: ночная работа (закрыл задачу
# в 01:00) относится к предыдущему дню, а не к наступившей календарной дате.
DAY_START_HOUR = 5
HISTORY_FIELDS = {
    "status",
    "assignee",
    "priority",
    "resolution",
    "timeoriginalestimate",
    "timeestimate",
    "timespent",
    "fix version",
    "fix version/s",
    "fixversions",
    "sprint",
}


def _utf8_streams() -> None:
    """Переключает консоль Windows на UTF-8, если поток это поддерживает."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def parse_day(value: str, today: date | None = None) -> date:
    """Разбирает YYYY-MM-DD, «сегодня» и «вчера»."""
    today = today or date.today()
    normalized = (value or "").strip().casefold()
    if normalized in {"сегодня", "today"}:
        return today
    if normalized in {"вчера", "yesterday"}:
        return today - timedelta(days=1)
    try:
        return date.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError(
            "Дата должна быть YYYY-MM-DD, «сегодня» или «вчера»: %s" % value
        ) from error


def resolve_period(
    day_value: str | None,
    since_value: str | None,
    until_value: str | None,
    timezone_name: str,
    today: date | None = None,
) -> tuple[date, date]:
    """Возвращает включительные границы отчёта и проверяет аргументы."""
    if today is None:
        today = logical_date(datetime.now(ZoneInfo(timezone_name)), timezone_name)
    if day_value:
        if since_value or until_value:
            raise ValueError("--date нельзя смешивать с --since/--until")
        day = parse_day(day_value, today=today)
        return day, day
    if not since_value or not until_value:
        raise ValueError("Задай --date либо одновременно --since и --until")
    since = parse_day(since_value, today=today)
    until = parse_day(until_value, today=today)
    if until < since:
        raise ValueError("--until не может быть раньше --since")
    return since, until


def parse_jira_datetime(value: Any) -> datetime | None:
    """Разбирает дату Jira с часовым поясом, Z или без смещения."""
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


def logical_date(moment: datetime, timezone_name: str) -> date:
    """Возвращает отчётный день момента: сутки начинаются в DAY_START_HOUR."""
    if moment.tzinfo is not None:
        moment = moment.astimezone(ZoneInfo(timezone_name))
    return (moment - timedelta(hours=DAY_START_HOUR)).date()


def date_in_period(
    value: Any,
    since: date,
    until: date,
    timezone_name: str,
) -> bool:
    """Проверяет, попадает ли дата Jira во включительный период."""
    parsed = parse_jira_datetime(value)
    if not parsed:
        return False
    return since <= logical_date(parsed, timezone_name) <= until


def display_period(since: date, until: date) -> str:
    """Форматирует период для заголовка."""
    if since == until:
        return since.strftime("%d.%m.%Y")
    return "%s–%s" % (since.strftime("%d.%m.%Y"), until.strftime("%d.%m.%Y"))


def jql_quote(value: str) -> str:
    """Экранирует строковое значение для JQL."""
    return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')


def build_default_jql(
    login: str,
    since: date,
    until: date,
    include_history: bool = True,
) -> str:
    """Строит JQL личной работы за период.

    Границы — не полночь, а DAY_START_HOUR (см. константу): ночная активность
    попадает в предыдущий отчётный день. `worklogDate` в JQL имеет точность до
    суток, поэтому берётся на день шире — точный отбор делает normalize_worklogs.

    Вариант с историей исполнителя точнее, но старые Jira могут отвергать DURING.
    Для них вызывающий повторяет запрос с include_history=False.
    """
    user = jql_quote(login)
    start = "%s %02d:00" % (since.isoformat(), DAY_START_HOUR)
    end = "%s %02d:00" % ((until + timedelta(days=1)).isoformat(), DAY_START_HOUR)
    actors = [
        (
            "(creator = %s AND created >= %s AND created < %s)"
            % (user, jql_quote(start), jql_quote(end))
        ),
        (
            "(worklogAuthor = %s AND worklogDate >= %s AND worklogDate <= %s)"
            % (
                user,
                jql_quote(since.isoformat()),
                jql_quote((until + timedelta(days=1)).isoformat()),
            )
        ),
    ]
    if include_history:
        # `assignee WAS X DURING` матчит и задачи, где исполнитель не менялся годами:
        # назначение «длится» сквозь окно. Без `updated >= начало` в дневной отчёт
        # попадает вся история задач пользователя. Верхней границы по updated нет
        # намеренно (см. report-format.md).
        actors.insert(
            1,
            "(assignee WAS %s DURING (%s, %s) AND updated >= %s)"
            % (user, jql_quote(start), jql_quote(end), jql_quote(start)),
        )
    else:
        actors.insert(
            1,
            "(assignee = %s AND updated >= %s)" % (user, jql_quote(start)),
        )
    return (
        "project = %s AND (%s) ORDER BY updated DESC"
        % (
            jira.CONFIG["project"],
            " OR ".join(actors),
        )
    )


def response_error(response: Any, operation: str) -> jira.StepError:
    """Преобразует HTTP-ответ в типизированную ошибку без утечки секретов."""
    detail = ""
    try:
        payload = response.json()
        detail = json.dumps(payload, ensure_ascii=False)[:2000]
    except Exception:
        detail = (getattr(response, "text", "") or "")[:2000]
    return jira.StepError(
        {
            "error": "jira_read_failed",
            "operation": operation,
            "status": getattr(response, "status_code", None),
            "detail": detail,
        }
    )


def search_issues(
    session: Any,
    jql: str,
    max_results: int,
) -> tuple[list[dict[str, Any]], int]:
    """Читает результаты JQL с пагинацией до заданного лимита."""
    start = 0
    issues: list[dict[str, Any]] = []
    total = 0
    while len(issues) < max_results:
        page_size = min(100, max_results - len(issues))
        response = jira.api(
            session,
            "GET",
            "/rest/api/2/search",
            check=False,
            params={
                "jql": jql,
                "startAt": start,
                "maxResults": page_size,
                "fields": REPORT_FIELDS,
            },
        )
        if response.status_code >= 400:
            raise response_error(response, "search")
        payload = response.json()
        chunk = payload.get("issues") or []
        total = int(payload.get("total") or 0)
        issues.extend(chunk)
        start += len(chunk)
        if not chunk or start >= total:
            break
    return issues, total


def list_comments(session: Any, key: str) -> list[dict[str, Any]]:
    """Читает комментарии задачи с пагинацией."""
    start = 0
    comments: list[dict[str, Any]] = []
    while True:
        response = jira.api(
            session,
            "GET",
            "/rest/api/2/issue/%s/comment" % key,
            check=False,
            params={"startAt": start, "maxResults": 100},
        )
        if response.status_code >= 400:
            raise response_error(response, "comments:%s" % key)
        payload = response.json()
        chunk = payload.get("comments") or []
        comments.extend(chunk)
        start += len(chunk)
        if not chunk or start >= int(payload.get("total") or len(comments)):
            return comments


def list_changelog(
    session: Any,
    key: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Читает changelog с пагинацией и фолбэком через expand."""
    warnings: list[str] = []
    response = jira.api(
        session,
        "GET",
        "/rest/api/2/issue/%s/changelog" % key,
        check=False,
        params={"startAt": 0, "maxResults": 100},
    )
    if response.status_code < 400:
        first = response.json()
        items = list(first.get("values") or first.get("histories") or [])
        total = int(first.get("total") or len(items))
        start = len(items)
        while start < total:
            page = jira.api(
                session,
                "GET",
                "/rest/api/2/issue/%s/changelog" % key,
                check=False,
                params={"startAt": start, "maxResults": 100},
            )
            if page.status_code >= 400:
                warnings.append(
                    "%s: changelog прочитан частично (%d из %d записей)."
                    % (key, len(items), total)
                )
                break
            data = page.json()
            chunk = data.get("values") or data.get("histories") or []
            items.extend(chunk)
            if not chunk:
                break
            start += len(chunk)
        return items, warnings

    fallback = jira.api(
        session,
        "GET",
        "/rest/api/2/issue/%s" % key,
        check=False,
        params={"fields": "summary", "expand": "changelog"},
    )
    if fallback.status_code >= 400:
        raise response_error(fallback, "changelog:%s" % key)
    changelog = fallback.json().get("changelog") or {}
    items = list(changelog.get("histories") or changelog.get("values") or [])
    total = int(changelog.get("total") or len(items))
    if total > len(items):
        warnings.append(
            "%s: Jira отдала через expand только %d из %d записей changelog."
            % (key, len(items), total)
        )
    else:
        warnings.append(
            "%s: отдельный endpoint changelog недоступен, использован expand."
            % key
        )
    return items, warnings


def actor_login(actor: Any) -> str | None:
    """Возвращает Jira-логин автора из разных форматов пользователя."""
    if not isinstance(actor, dict):
        return None
    return actor.get("name") or actor.get("key") or actor.get("accountId")


def actor_name(actor: Any) -> str:
    """Возвращает отображаемое имя автора."""
    if not isinstance(actor, dict):
        return "—"
    return (
        actor.get("displayName")
        or actor.get("name")
        or actor.get("key")
        or actor.get("accountId")
        or "—"
    )


def normalize_worklogs(
    raw_items: list[dict[str, Any]],
    key: str,
    login: str,
    since: date,
    until: date,
    timezone_name: str,
) -> list[dict[str, Any]]:
    """Оставляет worklog пользователя за период и приводит поля к контракту."""
    technical_seconds = jira.jira_time_seconds(
        jira.CONFIG.get("technical_worklog")
    )
    result: list[dict[str, Any]] = []
    for item in raw_items:
        author = item.get("author") or {}
        if actor_login(author) != login:
            continue
        started = item.get("started") or item.get("created")
        if not date_in_period(started, since, until, timezone_name):
            continue
        seconds = int(item.get("timeSpentSeconds") or 0)
        comment = item.get("comment")
        if isinstance(comment, dict):
            comment = json.dumps(comment, ensure_ascii=False)
        comment = str(comment or "").strip()
        result.append(
            {
                "id": item.get("id"),
                "issue_key": key,
                "started": started,
                "created": item.get("created"),
                "seconds": seconds,
                "display": item.get("timeSpent"),
                "comment": comment,
                "author_login": actor_login(author),
                "author": actor_name(author),
                "possible_technical": bool(
                    technical_seconds
                    and seconds == technical_seconds
                    and not comment
                ),
            }
        )
    return result


def normalize_comments(
    raw_items: list[dict[str, Any]],
    key: str,
    login: str,
    since: date,
    until: date,
    timezone_name: str,
) -> list[dict[str, Any]]:
    """Оставляет комментарии пользователя за период."""
    result: list[dict[str, Any]] = []
    for item in raw_items:
        author = item.get("author") or {}
        created = item.get("created")
        if actor_login(author) != login:
            continue
        if not date_in_period(created, since, until, timezone_name):
            continue
        body = item.get("body")
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False)
        result.append(
            {
                "id": item.get("id"),
                "issue_key": key,
                "created": created,
                "body": str(body or "").strip(),
                "author_login": actor_login(author),
                "author": actor_name(author),
            }
        )
    return result


def history_label(field: str) -> str:
    """Возвращает понятное название события changelog."""
    normalized = field.casefold()
    labels = {
        "status": "Статус изменён",
        "assignee": "Изменён исполнитель",
        "priority": "Изменён приоритет",
        "resolution": "Изменена резолюция",
        "timeoriginalestimate": "Изменена исходная оценка",
        "timeestimate": "Изменён остаток оценки",
        "timespent": "Изменено затраченное время",
        "fix version": "Изменена версия",
        "fix version/s": "Изменена версия",
        "fixversions": "Изменена версия",
        "sprint": "Изменён спринт",
    }
    return labels.get(normalized, "Изменено поле «%s»" % field)


def normalize_history(
    histories: list[dict[str, Any]],
    key: str,
    login: str,
    since: date,
    until: date,
    timezone_name: str,
) -> list[dict[str, Any]]:
    """Преобразует изменения пользователя за период в события отчёта."""
    result: list[dict[str, Any]] = []
    for history in histories:
        author = history.get("author") or {}
        created = history.get("created")
        if actor_login(author) != login:
            continue
        if not date_in_period(created, since, until, timezone_name):
            continue
        for item in history.get("items") or []:
            field = str(item.get("field") or "").strip()
            if field.casefold() not in HISTORY_FIELDS:
                continue
            before = item.get("fromString")
            after = item.get("toString")
            result.append(
                {
                    "type": (
                        "transition"
                        if field.casefold() == "status"
                        else "assignment"
                        if field.casefold() == "assignee"
                        else "field"
                    ),
                    "issue_key": key,
                    "timestamp": created,
                    "label": history_label(field),
                    "detail": "%s → %s" % (before or "—", after or "—"),
                    "value": field,
                    "author_login": actor_login(author),
                    "author": actor_name(author),
                }
            )
    return result


def parent_key(fields: dict[str, Any]) -> str | None:
    """Возвращает родителя по связи Parent-Child, если он присутствует."""
    for link in fields.get("issuelinks") or []:
        link_type = link.get("type") or {}
        if link_type.get("name") != LINK_TYPE:
            continue
        inward = link.get("inwardIssue")
        relation = str(link_type.get("inward") or "").casefold()
        if inward and "child" in relation:
            return inward.get("key")
    return None


def status_group(status: Any) -> str:
    """Сворачивает Jira-статус в один из пяти этапов отчёта."""
    status = status or {}
    name = str(status.get("name") or "")
    normalized = name.casefold()
    category = (status.get("statusCategory") or {}).get("key")
    if category == "done":
        return "done"
    if "приостанов" in normalized or "paused" in normalized:
        return "paused"
    if any(word in normalized for word in ("ревью", "review", "тест", "выполнена")):
        return "review"
    if category == "indeterminate" or any(
        word in normalized for word in ("работ", "разработ", "progress")
    ):
        return "progress"
    return "backlog"


def person_payload(value: Any) -> dict[str, Any] | None:
    """Нормализует пользователя Jira."""
    if not isinstance(value, dict):
        return None
    return {
        "login": actor_login(value),
        "name": actor_name(value),
    }


def normalize_issue(
    raw: dict[str, Any],
    login: str,
    worklogs: list[dict[str, Any]] | None = None,
    comments: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
    related_parent: bool = False,
) -> dict[str, Any]:
    """Приводит задачу Jira к стабильному контракту отчёта."""
    fields = raw.get("fields") or {}
    timing = fields.get("timetracking") or {}
    status = fields.get("status") or {}
    assignee = person_payload(fields.get("assignee"))
    issue_type = fields.get("issuetype") or {}
    priority = fields.get("priority") or {}
    epic_value = fields.get(jira.CONFIG["epic_link_field"])
    if isinstance(epic_value, dict):
        epic_value = epic_value.get("key") or epic_value.get("value")
    original = (
        fields.get("timeoriginalestimate")
        if fields.get("timeoriginalestimate") is not None
        else timing.get("originalEstimateSeconds")
    )
    remaining = (
        fields.get("timeestimate")
        if fields.get("timeestimate") is not None
        else timing.get("remainingEstimateSeconds")
    )
    spent = (
        fields.get("timespent")
        if fields.get("timespent") is not None
        else timing.get("timeSpentSeconds")
    )
    return {
        "key": raw.get("key"),
        "url": raw.get("url") or jira.issue_url(raw.get("key")),
        "summary": str(fields.get("summary") or "").strip(),
        "issue_type": issue_type.get("name") or "—",
        "status": status.get("name") or "—",
        "status_group": status_group(status),
        "priority": priority.get("name") or "—",
        "assignee": assignee,
        "reporter": person_payload(fields.get("reporter")),
        "creator": person_payload(fields.get("creator")),
        "created": fields.get("created"),
        "updated": fields.get("updated"),
        "resolution": (fields.get("resolution") or {}).get("name"),
        "resolutiondate": fields.get("resolutiondate"),
        "original_estimate_seconds": int(original or 0),
        "remaining_estimate_seconds": int(remaining or 0),
        "jira_time_spent_seconds": int(spent or 0),
        "fix_versions": [
            version.get("name")
            for version in fields.get("fixVersions") or []
            if version.get("name")
        ],
        "epic_key": epic_value,
        "parent_key": parent_key(fields),
        "assigned_to_report_user": bool(
            assignee and assignee.get("login") == login
        ),
        "related_parent": related_parent,
        "worklogs": worklogs or [],
        "comments": comments or [],
        "events": events or [],
    }


def read_parent_issue(session: Any, key: str) -> dict[str, Any]:
    """Читает поля связанного родителя без истории и worklog."""
    response = jira.api(
        session,
        "GET",
        "/rest/api/2/issue/%s" % key,
        check=False,
        params={"fields": REPORT_FIELDS},
    )
    if response.status_code >= 400:
        raise response_error(response, "parent:%s" % key)
    payload = response.json()
    return {
        "key": key,
        "url": jira.issue_url(key),
        "fields": payload.get("fields") or {},
    }


def issue_created_event(
    issue: dict[str, Any],
    login: str,
    since: date,
    until: date,
    timezone_name: str,
) -> dict[str, Any] | None:
    """Создаёт событие создания задачи, если её создал пользователь в периоде."""
    creator = issue.get("creator") or {}
    created = issue.get("created")
    if creator.get("login") != login:
        return None
    if not date_in_period(created, since, until, timezone_name):
        return None
    return {
        "type": "created",
        "issue_key": issue.get("key"),
        "timestamp": created,
        "label": "Задача создана",
        "detail": issue.get("summary") or "—",
        "value": issue.get("issue_type"),
        "author_login": creator.get("login"),
        "author": creator.get("name"),
    }


def worklog_events(worklogs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Преобразует worklog в события общей ленты."""
    return [
        {
            "type": "worklog",
            "issue_key": item.get("issue_key"),
            "timestamp": item.get("started"),
            "label": "Добавлен worklog",
            "detail": item.get("comment") or "Без комментария",
            "value": item.get("seconds") or 0,
            "author_login": item.get("author_login"),
            "author": item.get("author"),
            "possible_technical": item.get("possible_technical", False),
        }
        for item in worklogs
    ]


def comment_events(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Преобразует комментарии в события общей ленты."""
    return [
        {
            "type": "comment",
            "issue_key": item.get("issue_key"),
            "timestamp": item.get("created"),
            "label": "Добавлен комментарий",
            "detail": item.get("body") or "Пустой комментарий",
            "value": "comment",
            "author_login": item.get("author_login"),
            "author": item.get("author"),
        }
        for item in comments
    ]


def event_sort_key(event: dict[str, Any]) -> float:
    """Возвращает сортировочную дату события."""
    parsed = parse_jira_datetime(event.get("timestamp"))
    if not parsed:
        return float("-inf")
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=ZoneInfo("UTC")).timestamp()
    return parsed.timestamp()


def issue_completed_in_period(
    issue: dict[str, Any],
    since: date,
    until: date,
    timezone_name: str,
) -> bool:
    """Определяет завершение задачи в периоде по истории или resolutiondate.

    Передача в тестирование засчитывается как завершение: разработка по такой
    задаче окончена, дальше её ведёт тестировщик.
    """
    for event in issue.get("events") or []:
        if event.get("type") != "transition":
            continue
        detail = str(event.get("detail") or "")
        destination = detail.split("→", 1)[-1].casefold()
        # В changelog Jira статусы приходят по-английски («Ready for testing»),
        # а в полях задачи — по-русски. Ловим оба написания.
        if any(
            word in destination
            for word in (
                "выполн",
                "заверш",
                "done",
                "закры",
                "тестирован",
                "testing",
            )
        ):
            return True
    return date_in_period(
        issue.get("resolutiondate"),
        since,
        until,
        timezone_name,
    )


def build_stats(
    issues: list[dict[str, Any]],
    events: list[dict[str, Any]],
    since: date,
    until: date,
    timezone_name: str,
) -> dict[str, Any]:
    """Считает показатели только по рабочим задачам, исключая связанных родителей."""
    primary = [issue for issue in issues if not issue.get("related_parent")]
    worklogs = [
        item
        for issue in primary
        for item in issue.get("worklogs") or []
    ]
    status_counts = Counter(
        issue.get("status_group") or "backlog" for issue in primary
    )
    event_types = Counter(event.get("type") or "field" for event in events)
    created = sum(
        1
        for issue in primary
        if date_in_period(issue.get("created"), since, until, timezone_name)
    )
    completed = sum(
        1
        for issue in primary
        if issue_completed_in_period(issue, since, until, timezone_name)
    )
    active_keys = {
        event.get("issue_key")
        for event in events
        if event.get("issue_key")
    }
    return {
        "issues": len(primary),
        "assigned_issues": sum(
            1 for issue in primary if issue.get("assigned_to_report_user")
        ),
        "related_parents": sum(
            1 for issue in issues if issue.get("related_parent")
        ),
        "created": created,
        "completed": completed,
        "worklog_seconds": sum(int(item.get("seconds") or 0) for item in worklogs),
        "possible_technical_worklog_seconds": sum(
            int(item.get("seconds") or 0)
            for item in worklogs
            if item.get("possible_technical")
        ),
        "remaining_estimate_seconds": sum(
            int(issue.get("remaining_estimate_seconds") or 0)
            for issue in primary
            if issue.get("status_group") != "done"
        ),
        "original_estimate_seconds": sum(
            int(issue.get("original_estimate_seconds") or 0)
            for issue in primary
        ),
        "status_counts": {
            group: int(status_counts.get(group, 0))
            for group in ("backlog", "progress", "paused", "review", "done")
        },
        "active_issues": len(active_keys),
        "no_activity_issues": max(0, len(primary) - len(active_keys)),
        "events": len(events),
        "event_types": dict(event_types),
    }


def daily_worklog(
    issues: list[dict[str, Any]],
    since: date,
    until: date,
    timezone_name: str,
) -> list[dict[str, Any]]:
    """Агрегирует worklog по календарным дням периода."""
    totals: dict[date, int] = defaultdict(int)
    for issue in issues:
        if issue.get("related_parent"):
            continue
        for item in issue.get("worklogs") or []:
            parsed = parse_jira_datetime(item.get("started"))
            if not parsed:
                continue
            totals[logical_date(parsed, timezone_name)] += int(item.get("seconds") or 0)
    result = []
    cursor = since
    while cursor <= until:
        result.append({"date": cursor.isoformat(), "seconds": totals.get(cursor, 0)})
        cursor += timedelta(days=1)
    return result


def worklog_by_issue(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Агрегирует worklog по задачам, сохраняя нулевые задачи за бортом."""
    rows = []
    for issue in issues:
        if issue.get("related_parent"):
            continue
        items = issue.get("worklogs") or []
        seconds = sum(int(item.get("seconds") or 0) for item in items)
        if not seconds:
            continue
        rows.append(
            {
                "key": issue.get("key"),
                "summary": issue.get("summary"),
                "status": issue.get("status"),
                "status_group": issue.get("status_group"),
                "seconds": seconds,
                "entries": len(items),
            }
        )
    rows.sort(key=lambda row: row["seconds"], reverse=True)
    return rows


def collect_report(
    session: Any,
    since: date,
    until: date,
    timezone_name: str = "Europe/Moscow",
    assignee: str | None = None,
    jql: str | None = None,
    max_results: int = 100,
    include_parents: bool = False,
) -> dict[str, Any]:
    """Собирает полный нормализованный payload отчёта."""
    warnings: list[str] = []
    me_response = jira.api(session, "GET", "/rest/api/2/myself")
    me = me_response.json()
    login = assignee or actor_login(me)
    if not login:
        raise jira.StepError(
            {"error": "jira_user_unknown", "hint": "Не удалось определить Jira-логин."}
        )
    report_user = {
        "login": login,
        "name": actor_name(me) if not assignee or assignee == actor_login(me) else assignee,
    }

    requested_jql = jql
    actual_jql = jql or build_default_jql(login, since, until, include_history=True)
    try:
        raw_issues, total = search_issues(session, actual_jql, max_results)
    except jira.StepError:
        if requested_jql:
            raise
        actual_jql = build_default_jql(login, since, until, include_history=False)
        raw_issues, total = search_issues(session, actual_jql, max_results)
        warnings.append(
            "Jira отвергла JQL с историей исполнителя; использован совместимый запрос "
            "по текущему исполнителю, создателю и worklogAuthor."
        )
    if total > len(raw_issues):
        warnings.append(
            "JQL нашёл %d задач, но отчёт ограничен первыми %d (--max)."
            % (total, len(raw_issues))
        )

    issues: list[dict[str, Any]] = []
    all_events: list[dict[str, Any]] = []
    source_state = {
        "issues": "получено",
        "worklogs": "получено",
        "changelog": "получено",
        "comments": "получено",
    }

    for raw_issue in raw_issues:
        key = raw_issue.get("key")
        worklogs: list[dict[str, Any]] = []
        comments: list[dict[str, Any]] = []
        history_events: list[dict[str, Any]] = []
        try:
            worklogs = normalize_worklogs(
                jira.list_worklogs(session, key),
                key,
                login,
                since,
                until,
                timezone_name,
            )
        except (jira.StepError, ValueError) as error:
            source_state["worklogs"] = "частично"
            warnings.append("%s: не удалось прочитать worklog (%s)." % (key, error))
        try:
            comments = normalize_comments(
                list_comments(session, key),
                key,
                login,
                since,
                until,
                timezone_name,
            )
        except (jira.StepError, ValueError) as error:
            source_state["comments"] = "частично"
            warnings.append("%s: не удалось прочитать комментарии (%s)." % (key, error))
        try:
            histories, history_warnings = list_changelog(session, key)
            warnings.extend(history_warnings)
            history_events = normalize_history(
                histories,
                key,
                login,
                since,
                until,
                timezone_name,
            )
            if history_warnings:
                source_state["changelog"] = "частично"
        except (jira.StepError, ValueError) as error:
            source_state["changelog"] = "частично"
            warnings.append("%s: не удалось прочитать changelog (%s)." % (key, error))

        issue_events = history_events + worklog_events(worklogs) + comment_events(comments)
        issue = normalize_issue(
            {
                "key": key,
                "url": jira.issue_url(key),
                "fields": raw_issue.get("fields") or {},
            },
            login,
            worklogs=worklogs,
            comments=comments,
            events=issue_events,
        )
        created_event = issue_created_event(
            issue,
            login,
            since,
            until,
            timezone_name,
        )
        if created_event:
            issue_events.append(created_event)
        issue["events"] = sorted(issue_events, key=event_sort_key, reverse=True)
        # Кандидаты из JQL шире периода: `worklogDate` там с точностью до суток
        # (см. build_default_jql), поэтому ночная запись 27-го до 05:00 приводит
        # задачу 26-го дня. Без личных событий и без движения задачи внутри
        # отчётных суток в реестре ей делать нечего.
        if not issue["events"] and not date_in_period(
            issue.get("updated"), since, until, timezone_name
        ):
            continue
        all_events.extend(issue["events"])
        issues.append(issue)

    if include_parents:
        known = {issue.get("key") for issue in issues}
        missing_parents = []
        for issue in issues:
            key = issue.get("parent_key")
            if key and key not in known and key not in missing_parents:
                missing_parents.append(key)
        for key in missing_parents:
            try:
                parent = normalize_issue(
                    read_parent_issue(session, key),
                    login,
                    related_parent=True,
                )
                issues.append(parent)
                known.add(key)
            except (jira.StepError, ValueError) as error:
                warnings.append(
                    "%s: связанный родитель не добавлен (%s)." % (key, error)
                )

    all_events.sort(key=event_sort_key, reverse=True)
    possible_technical = sum(
        int(event.get("value") or 0)
        for event in all_events
        if event.get("type") == "worklog"
        and event.get("possible_technical")
    )
    if possible_technical:
        warnings.append(
            "Worklog ровно по %s без комментария отмечен как возможный технический; "
            "это эвристика, Jira не хранит отдельный признак."
            % jira.CONFIG.get("technical_worklog", "1m")
        )

    stats = build_stats(
        issues,
        all_events,
        since,
        until,
        timezone_name,
    )
    generated_at = datetime.now(ZoneInfo(timezone_name)).isoformat(timespec="seconds")
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "period": {
            "since": since.isoformat(),
            "until": until.isoformat(),
            "label": display_period(since, until),
            "timezone": timezone_name,
        },
        "scope": {
            "project": jira.CONFIG["project"],
            "report_user": report_user,
            "jql": actual_jql,
            "include_parents": include_parents,
            "max_results": max_results,
            "matched_total": total,
            "definition": (
                "Задачи с движением в периоде: исполнитель в периоде и задача "
                "обновлялась внутри него, создание задачи в периоде или worklog "
                "с датой внутри периода."
            ),
        },
        "stats": stats,
        "issues": issues,
        "events": all_events,
        "daily_worklog": daily_worklog(
            issues,
            since,
            until,
            timezone_name,
        ),
        "worklog_by_issue": worklog_by_issue(issues),
        "warnings": warnings,
        "sources": [
            {
                "name": "Список задач",
                "detail": actual_jql,
                "status": source_state["issues"],
            },
            {
                "name": "Worklog",
                "detail": "Постранично по каждой задаче; только записи пользователя за период.",
                "status": source_state["worklogs"],
            },
            {
                "name": "История изменений",
                "detail": "Changelog переходов, назначений, оценок, версий и спринтов.",
                "status": source_state["changelog"],
            },
            {
                "name": "Комментарии",
                "detail": "Только комментарии пользователя за период.",
                "status": source_state["comments"],
            },
        ],
    }


def output_paths(
    out_dir: Path,
    since: date,
    until: date,
) -> tuple[Path, Path]:
    """Возвращает пути HTML и опционального JSON."""
    suffix = since.isoformat()
    if until != since:
        suffix += "_%s" % until.isoformat()
    base = "Jira_%s_отчёт_%s" % (jira.CONFIG["project"], suffix)
    return out_dir / (base + ".html"), out_dir / (base + ".json")


def build_parser() -> argparse.ArgumentParser:
    """Создаёт CLI генератора."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="сегодня, вчера или YYYY-MM-DD")
    parser.add_argument("--since", help="Начало периода, YYYY-MM-DD")
    parser.add_argument("--until", help="Конец периода включительно, YYYY-MM-DD")
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="Каталог для готового HTML",
    )
    parser.add_argument(
        "--assignee",
        help="Jira-логин пользователя; по умолчанию текущий пользователь",
    )
    parser.add_argument(
        "--jql",
        help="Явный JQL вместо стандартной личной выборки",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=100,
        help="Максимум задач в отчёте",
    )
    parser.add_argument(
        "--include-parents",
        action="store_true",
        help="Добавить связанных родителей, не включая их в личную статистику",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Также сохранить нормализованный JSON рядом с HTML",
    )
    parser.add_argument(
        "--timezone",
        default="Europe/Moscow",
        help="Часовой пояс IANA для границ периода",
    )
    return parser


def main() -> int:
    """Запускает сбор данных и рендеринг."""
    _utf8_streams()
    args = build_parser().parse_args()
    if args.max < 1:
        print(
            json.dumps({"error": "--max должен быть больше нуля"}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    try:
        since, until = resolve_period(
            args.date,
            args.since,
            args.until,
            args.timezone,
        )
    except (ValueError, ZoneInfoNotFoundError) as error:
        print(
            json.dumps({"error": str(error)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2

    session = jira.get_session()
    try:
        payload = collect_report(
            session,
            since,
            until,
            timezone_name=args.timezone,
            assignee=args.assignee,
            jql=args.jql,
            max_results=args.max,
            include_parents=args.include_parents,
        )
    except jira.StepError as error:
        print(
            json.dumps(error.payload, ensure_ascii=False, indent=2),
            file=sys.stderr,
        )
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    html_path, json_path = output_paths(args.out, since, until)
    render_html(payload, html_path)
    if args.json:
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    result = {
        "ok": True,
        "period": payload["period"],
        "issues": payload["stats"]["issues"],
        "completed": payload["stats"]["completed"],
        "worklog_seconds": payload["stats"]["worklog_seconds"],
        "html": os.path.abspath(html_path),
        "json": os.path.abspath(json_path) if args.json else None,
        "warnings": payload["warnings"],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
