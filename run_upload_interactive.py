#!/usr/bin/env python3
"""
Удобный интерфейс: загружаем данные из Excel, выбираем страну из колонки Country,
указываем сколько комбо и аккаунт — выгружаем Excel с кампаниями под FB.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

_root = Path(__file__).resolve().parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
os.chdir(_root)

from config.paths import get_excel_path, get_upload_fb_excel_path
from run_analysis_then_upload import run as run_bridge


def main():
    stats_default = get_excel_path()
    upload_default = get_upload_fb_excel_path()
    out_default = str(_root / "output" / "upload_from_analysis.xlsx")

    print("=== Выгрузка кампаний из анализа ===\n")
    print("Док с отчётом (cursor example.xlsx). Enter = по умолчанию:")
    path_prompt = f"  [{stats_default}]\n  > "
    try:
        stats_path = input(path_prompt).strip()
    except EOFError:
        stats_path = ""
    stats_path = stats_path or stats_default

    if not os.path.isfile(stats_path):
        print(f"Файл не найден: {stats_path}")
        sys.exit(1)

    mtime = os.path.getmtime(stats_path)
    file_date = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
    print(f"Дата файла (последнее изменение): {file_date}\n")

    # Загружаем и получаем список стран из колонки Country
    import pandas as pd
    from src.analysis_agent.analyze_fb_stats import compute_metrics, enrich_structure, aggregate_combos

    df = pd.read_excel(stats_path)
    required = ["Country", "Account Alias", "Adset Name", "Ad Title", "# Leads", "# FTDs", "$ Spend", "CM", "Ad Asset", "$ Net"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        print(f"В файле нет колонок: {missing}")
        sys.exit(1)

    df = compute_metrics(df)
    df = enrich_structure(df)
    combos = aggregate_combos(df)

    # Короткий анализ: топ 2 связки по каждой стране (в терминал)
    from src.analysis_agent.analyze_fb_stats import print_short_report
    print_short_report(combos, top_per_country=2, df=df)

    # Страны строго из колонки Country (Country Final = df["Country"])
    countries = sorted(df["Country"].dropna().astype(str).unique().tolist())
    countries = [c.strip() for c in countries if c.strip() and c.lower() != "nan"]

    if not countries:
        print("В колонке Country нет значений.")
        sys.exit(1)

    print("\nСтраны (из колонки Country):")
    for i, c in enumerate(countries, 1):
        n_combos = (combos["Country Final"].astype(str).str.strip() == c).sum()
        print(f"  {i}. {c}  (комбо: {n_combos})")
    print("  Введи номер или название страны:")
    try:
        country_choice = input("  > ").strip()
    except EOFError:
        country_choice = ""
    if not country_choice:
        print("Страна не выбрана.")
        sys.exit(1)

    # Определяем выбранную страну
    if country_choice.isdigit() and 1 <= int(country_choice) <= len(countries):
        country = countries[int(country_choice) - 1]
    else:
        match = [c for c in countries if c.lower() == country_choice.lower()]
        if not match:
            match = [c for c in countries if country_choice.lower() in c.lower()]
        country = match[0] if match else country_choice
    print(f"  Выбрано: {country}")

    print("\nСколько комбо взять (Enter = 2):")
    try:
        top_s = input("  > ").strip()
    except EOFError:
        top_s = ""
    top_n = int(top_s) if top_s.isdigit() else 2
    top_n = max(1, top_n)

    print("\nИмя аккаунта для листа в выгрузке (например: FacebookUL574 GT BID):")
    try:
        account = input("  > ").strip()
    except EOFError:
        account = ""
    if not account:
        print("Аккаунт не указан.")
        sys.exit(1)

    print("\nВыгружаю...")
    run_bridge(
        stats_excel=stats_path,
        upload_excel=upload_default,
        account=account,
        output_path=out_default,
        country_filter=country,
        top_n=top_n,
        save_plan_csv=None,
    )
    print(f"\nГотово. Файл: {out_default}")


if __name__ == "__main__":
    main()
