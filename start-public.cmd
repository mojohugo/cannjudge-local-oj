@echo off
cd /d "%~dp0"
python -X utf8 scripts\serve_public.py
pause
