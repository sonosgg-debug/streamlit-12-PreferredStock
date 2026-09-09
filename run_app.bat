@echo off
echo ========================================================
echo   Preferred Stock Comparison Dashboard
echo ========================================================
echo.
cd /d "%~dp0"
python -m streamlit run app.py
pause
