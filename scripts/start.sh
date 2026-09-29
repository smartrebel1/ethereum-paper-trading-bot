#!/usr/bin/env bash
# أمر واحد بيشغّل كل حاجة: فحص الأمان → تجهيز قاعدة البيانات → استيراد بيانات
# بينانس → تشغيل المحاكاة → تجهيز اللوحة → تشغيل السيرفر.
#
#   bash scripts/start.sh              # أول تشغيل: التاريخ كله (حوالي 4 دقايق)
#   bash scripts/start.sh --quick      # تجربة سريعة: آخر 2000 شمعة فقط
#   bash scripts/start.sh --no-serve   # كل حاجة ماعدا تشغيل السيرفر
#   HOST=0.0.0.0 bash scripts/start.sh # لو عايز تفتحه من جهاز تاني على نفس الشبكة
#
# من الآمن تشغيله أكتر من مرة: كل خطوة لوحدها idempotent (بتتكرر من غير
# ما تكرّر بيانات أو صفقات).

set -euo pipefail

cd "$(dirname "$0")/.."

QUICK=0
SERVE=1
for arg in "$@"; do
  case "$arg" in
    --quick) QUICK=1 ;;
    --no-serve) SERVE=0 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "خيار غير معروف: $arg"; exit 1 ;;
  esac
done

if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
else
  echo "❌ مفيش python على الجهاز. نصّب Python 3.12+ الأول."
  exit 1
fi

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

echo "▶︎ 1/6  فحص الإعدادات وقواعد الأمان (وضع تجريبي فقط)…"
"$PY" scripts/check_env.py >/dev/null
echo "    ✅ الإعدادات سليمة: TRADING_MODE=paper (مفيش تداول حقيقي ممكن أصلاً)."

echo "▶︎ 2/6  تجهيز قاعدة البيانات (migrations)…"
"$PY" -m alembic upgrade head >/dev/null
echo "    ✅ قاعدة البيانات جاهزة."

count() {  # $1 = candles | trades
  "$PY" - "$1" <<'PY' 2>/dev/null || echo 0
import sys
from sqlalchemy import func, select

from app.database.session import session_scope
from app.models import Candle, Trade

model = {"candles": Candle, "trades": Trade}[sys.argv[1]]
with session_scope() as session:
    print(int(session.execute(select(func.count()).select_from(model)).scalar() or 0))
PY
}

CANDLES="$(count candles)"
if [ "${CANDLES:-0}" -eq 0 ]; then
  echo "▶︎ 3/6  استيراد بيانات بينانس (أول مرة: حوالي 3 ثواني)…"
  "$PY" scripts/ingest_archive.py | sed 's/^/    /'
else
  echo "▶︎ 3/6  البيانات موجودة بالفعل: $CANDLES شمعة (مش محتاجة استيراد)."
fi

TRADES="$(count trades)"
if [ "${TRADES:-0}" -eq 0 ]; then
  echo "▶︎ 4/6  تشغيل المحاكاة على البيانات…"
  if [ "$QUICK" -eq 1 ]; then
    "$PY" scripts/replay.py --limit 2000 | sed 's/^/    /'
  else
    "$PY" scripts/replay.py | sed 's/^/    /'
  fi
else
  echo "▶︎ 4/6  المحاكاة معمولة بالفعل: $TRADES صفقة مقفولة."
  if [ "$QUICK" -eq 1 ]; then
    echo "    (لو عايز تعيد التشغيل من الأول: python scripts/replay.py --rebuild-db)"
  fi
fi

echo "▶︎ 5/6  تجهيز ملف اللوحة (dashboard/dashboard_ar.html)…"
"$PY" scripts/render_dashboard.py | sed 's/^/    /'

if [ "$SERVE" -eq 0 ]; then
  echo
  echo "✅ خلص. افتح الملف: dashboard/dashboard_ar.html"
  exit 0
fi

echo "▶︎ 6/6  تشغيل السيرفر…"
echo
echo "┌──────────────────────────────────────────────────────────────┐"
echo "│  🟢 اللوحة بالعربي : http://${HOST}:${PORT}/dashboard"
echo "│  📊 اللوحة التقنية : http://${HOST}:${PORT}/dashboard/technical"
echo "│  📘 توثيق الواجهة : http://${HOST}:${PORT}/docs"
echo "│  (للإيقاف اضغط Ctrl + C)                                      │"
echo "└──────────────────────────────────────────────────────────────┘"
echo
exec "$PY" -m uvicorn app.main:app --host "$HOST" --port "$PORT" --log-level warning
