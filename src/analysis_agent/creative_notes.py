"""Рендер заметок Obsidian для дневника креативов.

Три заметки на страну, с жёсткой границей режимов:

* ``Креативы — {Страна} — Свод вердиктов`` — **генерируется целиком**, руками не
  правится: всё написанное вручную будет затёрто следующим прогоном.
* ``Креативы — {Страна} — Дневник`` — только дозапись, пишет человек или LLM.
  Создаётся один раз и после этого не трогается генератором.
* ``Креативы — {Страна} — Портрет ЦА`` — курируется. Тоже создаётся один раз.

Граница нужна потому, что смешанные файлы теряют ручные пометки при первой же
перегенерации, после чего файлу перестают доверять целиком.
"""
from __future__ import annotations

import datetime as dt

from .creative_verdicts import (
    BAD,
    GOOD,
    INSUFFICIENT,
    MATURITY_DAYS,
    MIN_FTD_FOR_ROI,
    MIN_LEADS_FOR_CR,
    NET_CAP_PER_FTD,
    RELATIVE_AFTER_DAYS,
    WATCH,
    Bundle,
    CountryStats,
    cpl_baseline,
    is_mature,
    judge_bundle,
    judge_rollup,
    rollup,
    spend_gate,
)

GENERATED_BANNER = (
    "> [!warning] Файл генерируется автоматически\n"
    "> Любые ручные правки будут затёрты при следующем прогоне"
    " `scripts/build_creative_notes.py`.\n"
    "> Наблюдения пишите в «Дневник», выводы о ЦА — в «Портрет ЦА»."
)


def _money(v: float | None) -> str:
    return "—" if v is None else f"${v:,.0f}"


def _money2(v: float | None) -> str:
    return "—" if v is None else f"${v:,.2f}"


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.2f}%"


def _ratio(v: float | None) -> str:
    return "—" if v is None else f"{v:.2f}"


def _frontmatter(created: str, updated: str, tags: str) -> str:
    return (
        "---\n"
        f"tags: [{tags}]\n"
        f"created: {created}\n"
        f"updated: {updated}\n"
        "status: active\n"
        "---\n"
    )


# ── свод вердиктов ───────────────────────────────────────────────────────────


def render_digest(
    country: str,
    bundles: dict[tuple, Bundle],
    stats: CountryStats,
    *,
    as_of: str,
    archive_gaps: list[str] | None = None,
    lag: dict | None = None,
    top_n: int = 25,
) -> str:
    today = dt.date.today().isoformat()
    items = list(bundles.values())
    baseline = cpl_baseline(items, stats, as_of)
    mature = [b for b in items if is_mature(b, stats, as_of)]
    gate = spend_gate(stats)

    out: list[str] = [
        _frontmatter(today, today, "creatives, creatives/verdicts"),
        f"\n# Креативы — {country} — Свод вердиктов\n",
        GENERATED_BANNER,
        f"\n**Данные на:** {as_of}  ",
        f"**История:** {stats.date_min} .. {stats.date_max}"
        f" ({stats.history_days} дн.)  ",
        f"**Связок всего:** {len(items):,}  ·  **дозрели:** {len(mature):,}\n",
    ]

    # ── экономика страны
    out.append("## Экономика страны\n")
    out.append("| Метрика | Значение |\n|---|---|")
    out.append(f"| Спенд | {_money(stats.spend)} |")
    out.append(f"| Лиды | {stats.leads:,} |")
    out.append(f"| Депозиты (FTD) | {stats.ftds:,} |")
    out.append(f"| Выручка (сырая) | {_money(stats.net_raw)} |")
    out.append(f"| Выручка (урезанная) | {_money(stats.net_capped)} |")
    out.append(f"| ROI по урезанной | {_ratio(stats.roi)} |")
    out.append(f"| CPL | {_money2(stats.cpl)} |")
    out.append(f"| CPA | {_money(stats.cpa)} |")
    out.append(f"| Конверсия лид→FTD | {_pct(stats.cr)} |\n")

    # ── качество данных
    out.append("## Качество данных\n")
    out.append(
        "Эти цифры — не косметика: они показывают, насколько вообще можно верить"
        " вердиктам ниже.\n"
    )
    out.append("| Проблема | Объём | Что сделано |\n|---|---|---|")
    out.append(
        f"| Спенд без оффера, возвращён на связки | {_money(stats.redistributed_spend)}"
        " | разнесён по адсету пропорционально |"
    )
    out.append(
        f"| **Спенд-сирота (слепое пятно)** | **{_money(stats.orphan_spend)}**"
        " | ни на что не отнесён, в вердиктах не участвует |"
    )
    out.append(
        f"| Адсеты с неразобранным именем | {stats.unparsed_adsets} из"
        f" {stats.total_adsets} | таргет/возраст неизвестны |"
    )
    if stats.spend:
        share = stats.orphan_spend / stats.spend * 100
        out.append(f"| Доля слепого пятна от спенда | {share:.1f}% | — |")
    out.append("")

    if archive_gaps:
        head = ", ".join(archive_gaps[:10])
        more = f" … и ещё {len(archive_gaps) - 10}" if len(archive_gaps) > 10 else ""
        out.append(
            f"> [!danger] Дыры в архиве: {len(archive_gaps)} дн.\n"
            f"> {head}{more}\n"
            "> За эти даты данных нет вообще — просадки по ним не настоящие.\n"
        )
    else:
        out.append("> [!success] Дыр в архиве нет — период непрерывен.\n")

    # ── правила
    out.append("## По каким правилам выданы вердикты\n")
    rule_mode = (
        "относительные границы по базе страны"
        if stats.history_days >= RELATIVE_AFTER_DAYS
        else f"абсолютные границы (истории < {RELATIVE_AFTER_DAYS} дн.)"
    )
    out.append(f"- **Режим границ:** {rule_mode}")
    out.append(
        f"- **Гейт зрелости:** спенд ≥ {_money(gate)} (2×CPA страны)"
        f" **и** ≥ {MATURITY_DAYS} дн. с последней открутки"
    )
    out.append(
        f"- **Связка судится по CPL** — база {_money2(baseline)};"
        " ROI на этом уровне не считается: одна строка выручки даёт до четверти"
        " всей суммы, ROI связки был бы шумом"
    )
    out.append(
        f"- **ROI и CPA** — только на срезах при ≥ {MIN_FTD_FOR_ROI} депозитах;"
        f" **лид→FTD** — при ≥ {MIN_LEADS_FOR_CR} лидах"
    )
    out.append(
        f"- **Винзоризация:** выручка урезана потолком {_money(NET_CAP_PER_FTD)}"
        " на депозит; возвраты не урезаны"
    )
    if lag and lag.get("samples"):
        out.append(
            f"- **Замер лага выручки:** медиана {lag['median']:.0f} дн.,"
            f" p90 {lag['p90']} дн., p95 {lag['p95']} дн."
            f" (наблюдений {lag['samples']:,}) — сверить с гейтом {MATURITY_DAYS} дн."
        )
    else:
        out.append(
            f"- ⚠️ **{MATURITY_DAYS} дн. зрелости взяты оценочно** — лаг ещё не"
            " измерен, нужно больше снапшотов"
        )
    out.append("")

    # ── роллапы
    out.append("## Срезы\n")
    for dim, title in (
        ("target", "По таргету"),
        ("age", "По возрасту"),
        ("offer", "По офферу"),
        ("variant", "По LP variant"),
        ("creative", "По креативу"),
    ):
        groups = rollup(items, dim)
        judged = []
        for label, group in groups.items():
            _, roi_v, cr_v = judge_rollup(label, group, stats)
            if roi_v or cr_v:
                judged.append((label, group, roi_v, cr_v))
        out.append(f"### {title}\n")
        if not judged:
            need = f"≥{MIN_FTD_FOR_ROI} депозитов или ≥{MIN_LEADS_FOR_CR} лидов"
            out.append(f"_Ни один срез не набрал статистики ({need})._\n")
            continue
        judged.sort(key=lambda t: -sum(b.spend for b in t[1]))
        out.append("| | Срез | Спенд | Лиды | FTD | ROI | лид→FTD | Основание |")
        out.append("|---|---|---|---|---|---|---|---|")
        for label, group, roi_v, cr_v in judged[:12]:
            sp = sum(b.spend for b in group)
            ld = sum(b.leads for b in group)
            fd = sum(b.ftds for b in group)
            mark = (roi_v or cr_v).mark
            roi_s = _ratio(roi_v.value) if roi_v else "—"
            cr_s = _pct(cr_v.value) if cr_v else "—"
            basis = (roi_v or cr_v).basis
            out.append(
                f"| {mark} | `{label}` | {_money(sp)} | {ld:,} | {fd} |"
                f" {roi_s} | {cr_s} | {basis} |"
            )
        out.append("")

    # ── связки
    out.append("## Связки\n")
    out.append(
        "Единица записи — оффер × таргет × возраст × креатив × LP variant."
        " Спенд указан **после** возврата неприписанного.\n"
    )
    scored = [(b, judge_bundle(b, stats, baseline, as_of)) for b in items]
    ranked = sorted(scored, key=lambda t: -t[0].spend)

    for section, marks in (
        ("Работают", (GOOD,)),
        ("Наблюдаем", (WATCH,)),
        ("Не работают", (BAD,)),
    ):
        rows = [(b, v) for b, v in ranked if v.mark in marks]
        out.append(f"### {section} ({len(rows)})\n")
        if not rows:
            out.append("_Пусто._\n")
            continue
        out.append("| | Оффер | Таргет | Возр. | Креатив | Variant | Спенд | Лиды | CPL | FTD | Основание |")
        out.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for b, v in rows[:top_n]:
            flag = " 🐋" if b.has_outlier else ""
            out.append(
                f"| {v.mark} | {b.offer} | `{b.target or '—'}` | {b.age or '—'} |"
                f" {b.creative} | `{b.variant or '—'}` | {_money(b.spend)} |"
                f" {b.leads:,} | {_money2(b.cpl)} | {b.ftds} | {v.basis}{flag} |"
            )
        if len(rows) > top_n:
            out.append(f"\n_Показано {top_n} из {len(rows)} по спенду._")
        out.append("")

    pending = [(b, v) for b, v in ranked if v.mark == INSUFFICIENT]
    out.append(f"### В тесте — вердикта нет ({len(pending)})\n")
    if pending:
        out.append(
            "Это **не** «не работает». Связки не прошли гейт зрелости; в бриф идут"
            " как гипотезы, а не как факт.\n"
        )
        out.append("| Оффер | Таргет | Креатив | Спенд | Лиды | CPL | Почему нет вердикта |")
        out.append("|---|---|---|---|---|---|---|")
        for b, v in pending[:10]:
            out.append(
                f"| {b.offer} | `{b.target or '—'}` | {b.creative} | {_money(b.spend)} |"
                f" {b.leads:,} | {_money2(b.cpl)} | {v.basis} |"
            )
        if len(pending) > 10:
            out.append(f"\n_Показано 10 из {len(pending)} по спенду._")
    else:
        out.append("_Пусто._")
    out.append("")

    whales = [b for b in items if b.has_outlier]
    if whales:
        out.append("## Киты\n")
        out.append(
            "Связки, где сырая выручка заметно выше урезанной. Вердикт считался по"
            " урезанной — иначе один депозит переворачивал бы оценку.\n"
        )
        out.append("| Оффер | Креатив | Спенд | FTD | Сырой net | Урезанный | ROI сырой |")
        out.append("|---|---|---|---|---|---|---|")
        for b in sorted(whales, key=lambda x: -(x.net_raw - x.net_capped))[:8]:
            out.append(
                f"| {b.offer} | {b.creative} | {_money(b.spend)} | {b.ftds} |"
                f" {_money(b.net_raw)} | {_money(b.net_capped)} | {_ratio(b.roi_raw)} |"
            )
        out.append("")

    out.append("## Связанные заметки\n")
    out.append(f"- [[Креативы — {country} — Дневник]]")
    out.append(f"- [[Креативы — {country} — Портрет ЦА]]")
    out.append("- [[Креативы — 00 Индекс]]")
    return "\n".join(out) + "\n"


# ── заготовки (создаются один раз, генератором не трогаются) ─────────────────


def render_diary_seed(country: str) -> str:
    today = dt.date.today().isoformat()
    return (
        _frontmatter(today, today, "creatives, creatives/diary")
        + f"\n# Креативы — {country} — Дневник\n\n"
        "> [!info] Только дозапись\n"
        "> Генератор этот файл не трогает. Новые записи — **сверху**.\n"
        "> Запись создаётся **до** запуска креатива, а не после: предрегистрация —"
        " единственное, что отличает обучение от объяснения задним числом.\n\n"
        "## Как заполнять\n\n"
        "Каждая запись — один бриф. Обязательные поля:\n\n"
        "- **Гипотеза** — что проверяем, одной фразой\n"
        "- **Связка** — оффер × таргет × возраст × креатив × LP variant\n"
        "- **На чём основано** — ссылка на строку свода или на внешний источник\n"
        "- **Ожидаемый результат** — конкретное число (CPL / ROI среза)\n"
        "- **Kill-rule** — при каком результате признаём провал и режем\n"
        "- **Итог** — дописывается позже, когда связка дозреет\n\n"
        "---\n\n"
        "_Записей пока нет._\n\n"
        "## Связанные заметки\n\n"
        f"- [[Креативы — {country} — Свод вердиктов]]\n"
        f"- [[Креативы — {country} — Портрет ЦА]]\n"
        "- [[Креативы — 00 Индекс]]\n"
    )


def render_audience_seed(country: str) -> str:
    today = dt.date.today().isoformat()
    return (
        _frontmatter(today, today, "creatives, creatives/audience")
        + f"\n# Креативы — {country} — Портрет ЦА\n\n"
        "> [!info] Курируется вручную\n"
        "> Генератор этот файл не трогает.\n\n"
        "## Внутренние сигналы\n\n"
        "_Из архива: какие таргеты, возрасты и связки реально приносят депозит._\n"
        "_Заполняется по своду вердиктов._\n\n"
        "## Внешние сигналы\n\n"
        "> [!caution] Внешнее — не факт о вашей аудитории\n"
        "> Каждое утверждение обязано нести источник и дату. Это результат"
        " веб-ресёрча, а не измерение: проверять на связке, прежде чем закладывать"
        " в серию креативов.\n\n"
        "| Утверждение | Источник | Дата | Проверено на связке |\n"
        "|---|---|---|---|\n"
        "| _пусто_ | | | |\n\n"
        "## Боли и искомые решения\n\n"
        "_Что человек пытается решить, приходя по нашей рекламе._\n\n"
        "## Связанные заметки\n\n"
        f"- [[Креативы — {country} — Свод вердиктов]]\n"
        f"- [[Креативы — {country} — Дневник]]\n"
        "- [[Креативы — 00 Индекс]]\n"
    )


def render_index(countries: list[str], archive_note: str) -> str:
    today = dt.date.today().isoformat()
    lines = [
        _frontmatter(today, today, "creatives"),
        "\n# Креативы — 00 Индекс\n",
        "> Память по созданию креативов: что уже работало, что нет, и на кого мы"
        " вообще целимся.\n",
        "## Как устроено\n",
        "Три заметки на страну, с разными режимами записи:\n",
        "| Заметка | Режим | Кто пишет |",
        "|---|---|---|",
        "| `— Свод вердиктов` | перезаписывается целиком | скрипт |",
        "| `— Дневник` | только дозапись | человек / LLM |",
        "| `— Портрет ЦА` | курируется | человек / LLM |",
        "",
        "Смешивать режимы в одном файле нельзя: перегенерация затрёт ручное, и"
        " файлу перестанут доверять.\n",
        "## Страны\n",
        "| Страна | Свод | Дневник | Портрет ЦА |",
        "|---|---|---|---|",
    ]
    for c in countries:
        lines.append(
            f"| {c} | [[Креативы — {c} — Свод вердиктов]] |"
            f" [[Креативы — {c} — Дневник]] | [[Креативы — {c} — Портрет ЦА]] |"
        )
    lines += [
        "",
        "## Архив данных\n",
        archive_note,
        "",
        "## Связанные заметки\n",
        "- [[00 - Индекс]]",
        "- [[Источники данных]]",
    ]
    return "\n".join(lines) + "\n"
