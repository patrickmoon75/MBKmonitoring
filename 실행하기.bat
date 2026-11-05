@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo 지원사업 모니터링을 시작합니다...
python monitor.py --dry-run
pause

