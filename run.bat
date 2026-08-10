@echo off
rem Запуск приложения отчётов по Jira. Логин и пароль берутся из переменных
rem окружения JIRA_USERNAME / JIRA_PASSWORD — здесь их прописывать не нужно.
cd /d "%~dp0"
python app.py
pause
