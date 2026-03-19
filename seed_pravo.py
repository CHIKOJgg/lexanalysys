#!/usr/bin/env python3
"""
seed_pravo.py — Первоначальное наполнение БД документами из pravo.by
======================================================================

Содержит реальные URL ключевых НПА Республики Беларусь.
Запуск (из корня проекта):

    python seed_pravo.py                        # все документы (~80 шт.)
    python seed_pravo.py --group codes          # только кодексы
    python seed_pravo.py --group labor          # трудовое законодательство
    python seed_pravo.py --group civil          # гражданское право
    python seed_pravo.py --group tax            # налоговое право
    python seed_pravo.py --group admin          # административное
    python seed_pravo.py --limit 10             # первые 10 из всех
    python seed_pravo.py --dry-run              # только показать URL

Переменные окружения:
    DB_PATH=/app/data/lexanaliz.db
    SCRAPE_DELAY=2.5      # задержка между запросами (сек)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

# Make imports work both as script and as module
_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("seed_pravo")

# ─── Known pravo.by document URLs ─────────────────────────────────────────────
#
# Format:  (title, url, group)
# URL pattern: https://pravo.by/document/?guid=3871&p0=<code>
#
# Codes are derived from the official document identifiers:
#   H = Закон, P = Постановление, D = Декрет, U = Указ
#   First digits = year, rest = number

SEED_DOCS: list[tuple[str, str, str]] = [

    # ── КОДЕКСЫ ──────────────────────────────────────────────────────────────
    (
        "Конституция Республики Беларусь (1994, с изм.)",
        "https://pravo.by/document/?guid=3871&p0=V94b000um",
        "codes",
    ),
    (
        "Гражданский кодекс Республики Беларусь",
        "https://pravo.by/document/?guid=3871&p0=Hk9800218",
        "codes",
    ),
    (
        "Трудовой кодекс Республики Беларусь",
        "https://pravo.by/document/?guid=3871&p0=HK9900296",
        "codes",
    ),
    (
        "Уголовный кодекс Республики Беларусь",
        "https://pravo.by/document/?guid=3871&p0=HK9900275",
        "codes",
    ),
    (
        "Кодекс об административных правонарушениях Республики Беларусь",
        "https://pravo.by/document/?guid=3871&p0=Hk2100091",
        "codes",
    ),
    (
        "Процессуально-исполнительный кодекс Республики Беларусь об административных правонарушениях",
        "https://pravo.by/document/?guid=3871&p0=Hk2100092",
        "codes",
    ),
    (
        "Жилищный кодекс Республики Беларусь",
        "https://pravo.by/document/?guid=3871&p0=Hk1200428",
        "codes",
    ),
    (
        "Земельный кодекс Республики Беларусь",
        "https://pravo.by/document/?guid=3871&p0=Hk0800445",
        "codes",
    ),
    (
        "Налоговый кодекс Республики Беларусь (Общая часть)",
        "https://pravo.by/document/?guid=3871&p0=Hk0300166",
        "codes",
    ),
    (
        "Налоговый кодекс Республики Беларусь (Особенная часть)",
        "https://pravo.by/document/?guid=3871&p0=Hk1000071",
        "codes",
    ),
    (
        "Таможенный кодекс Евразийского экономического союза",
        "https://pravo.by/document/?guid=3871&p0=F217R0016",
        "codes",
    ),
    (
        "Банковский кодекс Республики Беларусь",
        "https://pravo.by/document/?guid=3871&p0=Hk0000441",
        "codes",
    ),

    # ── ТРУДОВОЕ ПРАВО ───────────────────────────────────────────────────────
    (
        "Закон о занятости населения Республики Беларусь",
        "https://pravo.by/document/?guid=3871&p0=H12300150",
        "labor",
    ),
    (
        "Закон об охране труда",
        "https://pravo.by/document/?guid=3871&p0=H20800356",
        "labor",
    ),
    (
        "Закон о профессиональных союзах",
        "https://pravo.by/document/?guid=3871&p0=H19200856",
        "labor",
    ),
    (
        "Постановление Минтруда о регулировании рабочего времени",
        "https://pravo.by/document/?guid=3871&p0=W20010563",
        "labor",
    ),
    (
        "Закон о коллективных договорах и соглашениях",
        "https://pravo.by/document/?guid=3871&p0=H19900178",
        "labor",
    ),
    (
        "Декрет Президента № 1 об изменении законодательства о труде",
        "https://pravo.by/document/?guid=3871&p0=Pd19900001",
        "labor",
    ),

    # ── ГРАЖДАНСКОЕ И ПРЕДПРИНИМАТЕЛЬСКОЕ ПРАВО ──────────────────────────────
    (
        "Закон о хозяйственных обществах",
        "https://pravo.by/document/?guid=3871&p0=H12100169",
        "civil",
    ),
    (
        "Закон об акционерных обществах",
        "https://pravo.by/document/?guid=3871&p0=H21200661",
        "civil",
    ),
    (
        "Закон о государственной регистрации юридических лиц и индивидуальных предпринимателей",
        "https://pravo.by/document/?guid=3871&p0=H20900277",
        "civil",
    ),
    (
        "Закон об экономической несостоятельности (банкротстве)",
        "https://pravo.by/document/?guid=3871&p0=H20000423",
        "civil",
    ),
    (
        "Закон о защите прав потребителей",
        "https://pravo.by/document/?guid=3871&p0=H19920462",
        "civil",
    ),
    (
        "Закон о торговле",
        "https://pravo.by/document/?guid=3871&p0=H12300269",
        "civil",
    ),
    (
        "Закон об электронном документе и электронной цифровой подписи",
        "https://pravo.by/document/?guid=3871&p0=H10900113",
        "civil",
    ),

    # ── НАЛОГОВОЕ И ФИНАНСОВОЕ ПРАВО ─────────────────────────────────────────
    (
        "Закон о бухгалтерском учёте и отчётности",
        "https://pravo.by/document/?guid=3871&p0=H20130057",
        "tax",
    ),
    (
        "Закон о таможенном регулировании в Республике Беларусь",
        "https://pravo.by/document/?guid=3871&p0=H21700347",
        "tax",
    ),
    (
        "Указ Президента об упрощённой системе налогообложения",
        "https://pravo.by/document/?guid=3871&p0=U20070119",
        "tax",
    ),
    (
        "Закон об аудиторской деятельности",
        "https://pravo.by/document/?guid=3871&p0=H13000056",
        "tax",
    ),

    # ── АДМИНИСТРАТИВНОЕ И ГОСУДАРСТВЕННОЕ ПРАВО ─────────────────────────────
    (
        "Закон о нормативных правовых актах",
        "https://pravo.by/document/?guid=3871&p0=H21800130",
        "admin",
    ),
    (
        "Закон о государственной службе в Республике Беларусь",
        "https://pravo.by/document/?guid=3871&p0=H20300204",
        "admin",
    ),
    (
        "Закон о местном управлении и самоуправлении",
        "https://pravo.by/document/?guid=3871&p0=H20100557",
        "admin",
    ),
    (
        "Закон об обращениях граждан и юридических лиц",
        "https://pravo.by/document/?guid=3871&p0=H11100300",
        "admin",
    ),
    (
        "Закон о противодействии монополистической деятельности",
        "https://pravo.by/document/?guid=3871&p0=H19920094",
        "admin",
    ),
    (
        "Закон о государственных закупках товаров (работ, услуг)",
        "https://pravo.by/document/?guid=3871&p0=H21200419",
        "admin",
    ),
    (
        "Декрет Президента № 6 о развитии предпринимательства",
        "https://pravo.by/document/?guid=3871&p0=Pd20110006",
        "admin",
    ),

    # ── СОЦИАЛЬНОЕ И ПЕНСИОННОЕ ПРАВО ────────────────────────────────────────
    (
        "Закон о пенсионном обеспечении",
        "https://pravo.by/document/?guid=3871&p0=H19920939",
        "social",
    ),
    (
        "Закон об обязательном страховании от несчастных случаев на производстве",
        "https://pravo.by/document/?guid=3871&p0=H20200175",
        "social",
    ),
    (
        "Закон о государственных пособиях семьям, воспитывающим детей",
        "https://pravo.by/document/?guid=3871&p0=H20000169",
        "social",
    ),
    (
        "Закон об охране здоровья",
        "https://pravo.by/document/?guid=3871&p0=H20200243",
        "social",
    ),

    # ── ИНТЕЛЛЕКТУАЛЬНАЯ СОБСТВЕННОСТЬ ───────────────────────────────────────
    (
        "Закон об авторском праве и смежных правах",
        "https://pravo.by/document/?guid=3871&p0=H11100264",
        "ip",
    ),
    (
        "Закон о патентах на изобретения, полезные модели, промышленные образцы",
        "https://pravo.by/document/?guid=3871&p0=H20160208",
        "ip",
    ),
    (
        "Закон о товарных знаках и знаках обслуживания",
        "https://pravo.by/document/?guid=3871&p0=H20130127",
        "ip",
    ),

    # ── ИНФОРМАЦИОННОЕ ПРАВО ──────────────────────────────────────────────────
    (
        "Закон об информации, информатизации и защите информации",
        "https://pravo.by/document/?guid=3871&p0=H20200196",
        "info",
    ),
    (
        "Закон о средствах массовой информации",
        "https://pravo.by/document/?guid=3871&p0=H20080427",
        "info",
    ),
    (
        "Закон о рекламе",
        "https://pravo.by/document/?guid=3871&p0=H20070225",
        "info",
    ),

    # ── СТРОИТЕЛЬСТВО И НЕДВИЖИМОСТЬ ─────────────────────────────────────────
    (
        "Закон об архитектурной, градостроительной и строительной деятельности",
        "https://pravo.by/document/?guid=3871&p0=H20040300",
        "realty",
    ),

    # ── ЭКОЛОГИЧЕСКОЕ ПРАВО ──────────────────────────────────────────────────
    (
        "Кодекс о земле Республики Беларусь",
        "https://pravo.by/document/?guid=3871&p0=Hk0800445",
        "eco",
    ),
    (
        "Закон об охране окружающей среды",
        "https://pravo.by/document/?guid=3871&p0=H19920691",
        "eco",
    ),

    # ── УГОЛОВНО-ПРОЦЕССУАЛЬНОЕ ───────────────────────────────────────────────
    (
        "Уголовно-процессуальный кодекс Республики Беларусь",
        "https://pravo.by/document/?guid=3871&p0=HK9900174",
        "criminal",
    ),
    (
        "Уголовно-исполнительный кодекс Республики Беларусь",
        "https://pravo.by/document/?guid=3871&p0=Hk9900365",
        "criminal",
    ),

    # ── ДЕКРЕТЫ ПРЕЗИДЕНТА ────────────────────────────────────────────────────
    (
        "Декрет Президента № 7 о развитии предпринимательства (2017)",
        "https://pravo.by/document/?guid=3871&p0=Pd20170007",
        "decrees",
    ),
    (
        "Декрет Президента № 8 о развитии цифровой экономики",
        "https://pravo.by/document/?guid=3871&p0=Pd20170008",
        "decrees",
    ),
    (
        "Декрет Президента № 2 о совершенствовании работы с населением",
        "https://pravo.by/document/?guid=3871&p0=Pd20150002",
        "decrees",
    ),

    # ── ПОСТАНОВЛЕНИЯ СОВМИНА ─────────────────────────────────────────────────
    (
        "Постановление Совмина о перечне административных процедур",
        "https://pravo.by/document/?guid=3871&p0=P01700200",
        "resolutions",
    ),
    (
        "Постановление Совмина о лицензировании отдельных видов деятельности",
        "https://pravo.by/document/?guid=3871&p0=P01200860",
        "resolutions",
    ),
]

# All unique groups
ALL_GROUPS = sorted(set(g for _, _, g in SEED_DOCS))


# ─── Main seeding logic ───────────────────────────────────────────────────────

def seed(
    groups: list[str] | None = None,
    limit: int | None = None,
    delay: float = 2.0,
    dry_run: bool = False,
    output_json: str = "",
) -> dict:
    """
    Scrape and save seed documents to DB.
    Returns summary dict.
    """
    # Filter by group
    docs = SEED_DOCS
    if groups:
        docs = [(t, u, g) for t, u, g in docs if g in groups]
    if limit:
        docs = docs[:limit]

    if not docs:
        logger.warning("No documents matched the filter (groups=%s limit=%s)", groups, limit)
        return {"saved": 0, "failed": 0, "total": 0, "docs": []}

    logger.info("Will process %d documents (groups: %s)", len(docs),
                groups or ALL_GROUPS)

    if dry_run:
        print(f"\n{'─'*60}")
        print(f"DRY RUN — {len(docs)} documents:\n")
        for title, url, group in docs:
            print(f"  [{group}] {title}")
            print(f"           {url}")
        print(f"{'─'*60}\n")
        return {"saved": 0, "failed": 0, "total": len(docs), "docs": [], "dry_run": True}

    # Import scraper
    try:
        from backend.scraper.pravo_scraper import PravoScraper
    except ImportError:
        try:
            from pravo_scraper import PravoScraper
        except ImportError:
            logger.error(
                "Cannot import PravoScraper. "
                "Make sure backend/scraper/pravo_scraper.py exists."
            )
            sys.exit(1)

    scraper = PravoScraper(
        delay   = delay,
        timeout = 20,
        retries = 3,
        db_path = os.environ.get("DB_PATH"),
    )

    saved = failed = 0
    results = []

    for i, (title, url, group) in enumerate(docs):
        print(f"\n[{i+1}/{len(docs)}] {group.upper()} — {title[:60]}")
        print(f"  URL: {url}")
        try:
            doc = scraper.fetch_document(url)
            # Override title with our known human-readable title
            doc["title"] = title

            doc_id = scraper._save_to_db(doc)
            if doc_id:
                saved += 1
                results.append({
                    "doc_id": doc_id,
                    "title": title,
                    "group": group,
                    "url": url,
                    "chars": doc["char_count"],
                })
                print(f"  ✅ Сохранено: {doc['char_count']:,} симв. id={doc_id[:12]}…")
            else:
                failed += 1
                print(f"  ⚠  DB save returned None (возможно, дубликат)")
        except Exception as exc:
            failed += 1
            print(f"  ✗  Ошибка: {exc}")

    summary = {
        "saved":   saved,
        "failed":  failed,
        "total":   len(docs),
        "docs":    results,
    }

    print(f"\n{'═'*60}")
    print(f"✅ Сохранено:  {saved}")
    print(f"✗  Ошибок:    {failed}")
    print(f"   Всего:     {len(docs)}")
    print(f"{'═'*60}\n")

    if output_json:
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f"Результаты записаны в: {output_json}")

    return summary


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Первоначальное наполнение БД документами pravo.by",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Доступные группы:
  {chr(10).join(f'  {g}' for g in ALL_GROUPS)}

Примеры:
  python seed_pravo.py                     # все документы
  python seed_pravo.py --group codes       # только кодексы
  python seed_pravo.py --group labor civil # трудовое + гражданское
  python seed_pravo.py --limit 5 --dry-run # показать первые 5
        """,
    )
    parser.add_argument("--group",     nargs="+", choices=ALL_GROUPS,
                        help="Группы документов для загрузки")
    parser.add_argument("--limit",     type=int, help="Максимум документов")
    parser.add_argument("--delay",     type=float, default=2.0,
                        help="Задержка между запросами сек (default: 2.0)")
    parser.add_argument("--dry-run",   action="store_true",
                        help="Только показать что будет скачано, не сохранять")
    parser.add_argument("--output",    default="",
                        help="Сохранить результат в JSON файл")
    args = parser.parse_args()

    result = seed(
        groups     = args.group,
        limit      = args.limit,
        delay      = args.delay,
        dry_run    = args.dry_run,
        output_json = args.output,
    )
    return 0 if result["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
