#!/usr/bin/env python3
"""
run.py — запуск ЛексАнализ локально
=====================================
  python run.py              # просто запустить
  python run.py --seed       # сначала загрузить кодексы из pravo.by, потом запустить
  python run.py --port 8080  # другой порт
"""
import os, sys, argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# ── Загрузить .env ────────────────────────────────────────────────────────────
env_file = ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

# ── Создать папку для БД ──────────────────────────────────────────────────────
db_path = os.environ.get("DB_PATH", "backend/data/lexanaliz.db")
db_abs  = (ROOT / db_path).resolve()
db_abs.parent.mkdir(parents=True, exist_ok=True)
os.environ["DB_PATH"] = str(db_abs)

# ── Аргументы ────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
parser.add_argument("--seed", action="store_true", help="Загрузить кодексы из pravo.by перед запуском")
parser.add_argument("--group", nargs="+", default=["codes"], help="Группы НПА для seed (codes labor civil tax admin ...)")
args = parser.parse_args()

# ── Проверки ─────────────────────────────────────────────────────────────────
key = os.environ.get("OPENROUTER_API_KEY", "")
print("\n" + "="*55)
print("  ЛексАнализ v3")
print("="*55)
print(f"  Порт:  {args.port}")
print(f"  БД:    {db_abs}")
print(f"  Ключ:  {'✅ задан' if key else '❌ НЕ ЗАДАН — добавьте в .env'}")
print("="*55 + "\n")

if not key:
    print("ОШИБКА: OPENROUTER_API_KEY не задан в .env")
    print("Добавьте строку: OPENROUTER_API_KEY=sk-or-v1-...")
    sys.exit(1)

# ── Seed БД если нужно ───────────────────────────────────────────────────────
if args.seed:
    print(f"📥 Загрузка НПА из pravo.by (группы: {args.group})...")
    print("   Это займёт несколько минут, подождите...\n")
    from seed_pravo import seed
    result = seed(groups=args.group, delay=2.5)
    print(f"\n✅ Загружено: {result['saved']} | Ошибок: {result['failed']}")
    print("   Теперь можно загружать документы и они будут сравниваться с базой\n")

# ── Запуск ────────────────────────────────────────────────────────────────────
import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
# Заглушить лишние логи
logging.getLogger("werkzeug").setLevel(logging.WARNING)

from backend.server import create_app
app = create_app()

print(f"🚀 Открывайте: http://localhost:{args.port}\n")
app.run(host="0.0.0.0", port=args.port, debug=False, use_reloader=False)