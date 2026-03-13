@echo off
cd /d "%~dp0"
python run_jira_from_accounts.py --api
pause
