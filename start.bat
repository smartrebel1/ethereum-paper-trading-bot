@echo off
REM ============================================================
REM  تشغيل بوت التداول التجريبي + لوحة النتائج  (ويندوز)
REM  نفس خطوات ملف scripts\start.sh بس بصيغة الويندوز
REM  الاستخدام: انقر عليه مرتين، أو من الشاشة السوداء:  start.bat
REM ============================================================
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
  set PY=.venv\Scripts\python.exe
) else (
  set PY=python
)

echo [1/6] فحص الاعدادات وقواعد الامان...
%PY% scripts\check_env.py || goto :error

echo [2/6] تجهيز قاعدة البيانات...
%PY% -m alembic upgrade head || goto :error

echo [3/6] استيراد بيانات بينانس (لو لازم)...
%PY% scripts\ingest_archive.py || goto :error

echo [4/6] تشغيل المحاكاة على التاريخ...
%PY% scripts\replay.py || goto :error

echo [5/6] تجهيز ملف اللوحة...
%PY% scripts\render_dashboard.py || goto :error

echo [6/6] تشغيل السيرفر...
echo.
echo   افتح في المتصفح:  http://127.0.0.1:8000/dashboard
echo   للايقاف: Ctrl + C
echo.
%PY% -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --log-level warning
goto :eof

:error
echo.
echo   حدث خطأ في الخطوة السابقة. راجع الرسالة اللي فوق.
pause
