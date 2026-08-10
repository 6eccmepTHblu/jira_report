#!/usr/bin/env python3
"""Проверка раскладки worklog по дням и по графику рабочего дня.

    python test_xlsx_report.py
"""

from datetime import date, time

import xlsx_report


def payload(worklogs):
    return {
        "period": {"since": "2026-08-03", "until": "2026-08-07"},
        "scope": {"report_user": {"login": "ulanovvs", "name": "Уланов В.С."}},
        "issues": [{
            "key": "ATOM-1", "url": "http://x/ATOM-1", "summary": "Тема",
            "status": "Выполнена", "status_group": "done", "worklogs": worklogs,
        }],
    }


def wl(started, seconds, author="ulanovvs", comment=""):
    return {"started": started, "seconds": seconds, "author_login": author, "comment": comment}


def test_night_worklog_belongs_to_previous_day():
    """Списание в 00:16 относится к предыдущему рабочему дню: сутки начинаются в 05:00."""
    days = xlsx_report.collect_days(payload([wl("2026-08-07T00:16:00+05:00", 3600)]))
    assert [str(day) for day, _ in days] == ["2026-08-06"], days


def test_worklog_outside_period_dropped():
    """08.08 в 01:42 — это рабочий день 07.08, а 09.08 в 12:00 уже вне периода."""
    days = xlsx_report.collect_days(payload([
        wl("2026-08-08T01:42:00+05:00", 3600),
        wl("2026-08-09T12:00:00+05:00", 3600),
    ]))
    assert [str(day) for day, _ in days] == ["2026-08-07"], days


def test_foreign_worklog_dropped():
    """Чужие списания в личный отчёт не попадают."""
    days = xlsx_report.collect_days(payload([wl("2026-08-03T10:00:00+05:00", 3600, "someone")]))
    assert days == [], days


def test_rows_run_back_to_back_from_work_start():
    """Задачи дня идут цепочкой от начала рабочего дня, график дня — их сумма."""
    days = xlsx_report.collect_days(payload([
        wl("2026-08-03T19:09:00+05:00", 6 * 3600),
        wl("2026-08-03T22:00:00+05:00", 3 * 3600),
    ]))
    rows = xlsx_report.build_rows(days, time(9, 0))
    assert [(r["start"], r["finish"]) for r in rows] == [("09:00", "15:00"), ("15:00", "18:00")]
    assert (rows[0]["day_start"], rows[0]["day_end"]) == ("09:00", "18:00")
    assert rows[1]["day_start"] is None and rows[0]["day_rows"] == 2


def test_blocker_only_for_paused():
    """«Блокирующие факторы» заполняются только у приостановленных задач."""
    data = payload([wl("2026-08-03T10:00:00+05:00", 60, comment="UI ещё не готов")])
    data["issues"][0]["status_group"] = "paused"
    assert xlsx_report.collect_days(data)[0][1][0]["blocker"] == "UI ещё не готов"
    data["issues"][0]["status_group"] = "done"
    assert xlsx_report.collect_days(data)[0][1][0]["blocker"] == ""


def test_activity_keeps_every_action_and_humanizes_time():
    """Лист «Все действия» берёт события всех типов, чужие отбрасывает, секунды переводит."""
    data = payload([wl("2026-08-03T10:00:00+05:00", 3600)])
    data["issues"][0]["events"] = [
        {"type": "worklog", "timestamp": "2026-08-03T10:00:00+05:00", "label": "Добавлен worklog",
         "value": 3600, "detail": "", "author_login": "ulanovvs"},
        {"type": "field", "timestamp": "2026-08-03T11:00:00+05:00", "label": "Изменён остаток",
         "value": "timeestimate", "detail": "14400 → 12000", "author_login": "ulanovvs"},
        {"type": "transition", "timestamp": "2026-08-03T09:00:00+05:00", "label": "Статус изменён",
         "value": "status", "detail": "To Do → In work", "author_login": "someone"},
    ]
    rows = xlsx_report.collect_activity(data)
    assert [r["action"] for r in rows] == ["Добавлен worklog", "Изменён остаток"], rows
    assert rows[0]["detail"] == "1 ч", rows[0]
    assert rows[1]["detail"] == "4 ч → 3.33 ч", rows[1]


def test_jql_keeps_all_five_signals():
    """Выборка ловит и действия по чужим задачам: смену статуса и смену исполнителя."""
    import app

    query = app.personal_jql("ulanovvs", date(2026, 7, 18), date(2026, 8, 2))
    for signal in ("creator =", "assignee WAS", "worklogAuthor =",
                   "status CHANGED BY", "assignee CHANGED BY"):
        assert signal in query, signal
    assert query.endswith("ORDER BY updated DESC"), query
    # Границы периода — 05:00, а не полночь: ночная работа остаётся в своём рабочем дне.
    assert '"2026-07-18 05:00"' in query and '"2026-08-03 05:00"' in query, query


if __name__ == "__main__":
    for name, check in sorted(globals().items()):
        if name.startswith("test_"):
            check()
            print("ok", name)
