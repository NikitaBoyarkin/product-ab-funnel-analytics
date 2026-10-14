# Выручка выросла ×10 за год при стабильных возвратах 2.63% — но воронка теряет 72% пользователей ровно между активацией и оплатой

**Дата:** 2026-10-14 · **Нога:** `sql` (Leg 01) · **Источник:** `data/raw/{users,events,transactions}.csv` (синтетика, seed=42) · **SQL:** [`sql/01_sql.sql`](../../sql/01_sql.sql) · **Скрипт:** [`sql/run_01_sql.py`](../../sql/run_01_sql.py) · **Результаты:** [`sql/results/`](../../sql/results)

---

## TL;DR — 5 запросов, 11848490 ₽ чистой выручки, узкое место воронки — шаг `activation → paid_subscription` (35.17%)

Пять чистых срезов DuckDB поверх сырых CSV отвечают на бизнес-вопрос брифа с трёх сторон: **деньги** (месячная выручка, когорты, тарифы) и **воронка** (шаги, каналы). Все запросы сходятся с контрактом из `docs/insights/00-eda.md` до рубля: gross **12205060** ₽, возвраты **356570** ₽, net **11848490** ₽, **16477** транзакций, **433** возврата, **12000** пользователей, **3384** плательщика.

- **Выручка растёт почти линейно** с притоком плательщиков: январь **158910** ₽ → декабрь **1633000** ₽ чистыми (×10.28), а не за счёт роста чека — AOV фиксирован тарифом.
- **Качество выручки стабильно:** доля возвратов держится в коридоре **0.71% – 3.04%**, максимум в мае **3.04%**, итог за год **2.63%** — тренда на ухудшение нет.
- **Узкое место — один шаг:** `signup → activation` проходит **80.19%**, а `activation → paid_subscription` — только **35.17%**; на целевой конверсии теряется **6239** из **8616** неплативших.
- **Лучший канал по конверсии — `referral`** (**33.65%** paid, **41.88%** paid given activated) и по ARPU **1150.35** ₽, но по абсолютной выручке лидирует `organic` (**3294570** ₽).
- **Когорты быстро «выдыхаются»:** payer-rate падает с **32.87%** (март) до **19.75%** (декабрь), а ARPU когорты — с **1626.68** ₽ до **143.99** ₽; декабрь цензурирован (окно кончается 31.12).

---

## Q1. Месячная выручка растёт ×10, а возвраты не растут — качество денег не портится

**Бизнес-вопрос:** как меняется чистая выручка по месяцам и не растёт ли доля возвратов со временем?

```sql
SELECT
    date_trunc('month', transaction_date)                        AS month,
    ROUND(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 2)   AS gross_revenue_rub,
    ROUND(-SUM(CASE WHEN amount < 0 THEN amount ELSE 0 END), 2)  AS refunds_rub,
    ROUND(SUM(amount), 2)                                        AS net_revenue_rub,
    COUNT(*)                                                     AS txn_cnt,
    COUNT(*) FILTER (WHERE is_refund)                            AS refund_cnt,
    ROUND(100.0 * COUNT(*) FILTER (WHERE is_refund) / COUNT(*), 2) AS refund_rate_pct
FROM read_csv_auto('data/raw/transactions.csv')
GROUP BY 1 ORDER BY 1;
```

| month | gross_revenue_rub | refunds_rub | net_revenue_rub | txn_cnt | refund_cnt | refund_rate_pct |
|---|---:|---:|---:|---:|---:|---:|
| 2024-01 | 160990 | 2080 | 158910 | 223 | 2 | 0.90 |
| 2024-02 | 314080 | 1870 | 312210 | 425 | 3 | 0.71 |
| 2024-03 | 589310 | 18200 | 571110 | 799 | 20 | 2.50 |
| 2024-04 | 786640 | 24200 | 762440 | 1066 | 30 | 2.81 |
| 2024-05 | 938780 | 28110 | 910670 | 1281 | 39 | 3.04 |
| 2024-06 | 1022650 | 32190 | 990460 | 1386 | 41 | 2.96 |
| 2024-07 | 1142870 | 33080 | 1109790 | 1545 | 42 | 2.72 |
| 2024-08 | 1217810 | 37460 | 1180350 | 1643 | 44 | 2.68 |
| 2024-09 | 1316600 | 36740 | 1279860 | 1766 | 46 | 2.60 |
| 2024-10 | 1484110 | 43020 | 1441090 | 1997 | 48 | 2.40 |
| 2024-11 | 1546150 | 47550 | 1498600 | 2080 | 55 | 2.64 |
| 2024-12 | 1685070 | 52070 | 1633000 | 2266 | 63 | 2.78 |

**Инсайт.** Чистая выручка выросла с **158910** ₽ в январе до **1633000** ₽ в декабре — ×10.28, то есть рост почти полностью объясняется накоплением плательщиков и продлений (`txn_cnt` 223 → 2266, ×10.16), а не ростом среднего чека. Доля возвратов при этом колеблется в узком коридоре **0.71% – 3.04%** без монотонного тренда: максимум в мае (**3.04%**), к декабрю она даже ниже майской (**2.78%**), итог за год — **2.63%**. Практический вывод: деньги масштабируются, качество выручки стабильно, и отдельного «месяца-провала» по возвратам нет.

---

## Q2. Воронка теряет 72% именно на целевом шаге: `activation → paid_subscription` = 35.17%

**Бизнес-вопрос:** сколько уникальных пользователей проходит каждый шаг воронки `signup → activation → paid_subscription` и какая конверсия на каждом переходе?

```sql
WITH s AS (
    SELECT
        COUNT(DISTINCT user_id) FILTER (WHERE event_name = 'signup')            AS signup_users,
        COUNT(DISTINCT user_id) FILTER (WHERE event_name = 'activation')        AS activation_users,
        COUNT(DISTINCT user_id) FILTER (WHERE event_name = 'paid_subscription') AS paid_users
    FROM read_csv_auto('data/raw/events.csv')
)
SELECT * FROM (
    SELECT 1 AS step_order, 'signup' AS step, signup_users AS users,
           NULL::DOUBLE AS step_conv_pct, NULL::DOUBLE AS conv_from_signup_pct FROM s
    UNION ALL
    SELECT 2, 'activation', activation_users,
           ROUND(100.0 * activation_users / signup_users, 2),
           ROUND(100.0 * activation_users / signup_users, 2) FROM s
    UNION ALL
    SELECT 3, 'paid_subscription', paid_users,
           ROUND(100.0 * paid_users / activation_users, 2),
           ROUND(100.0 * paid_users / signup_users, 2) FROM s
) ORDER BY step_order;
```

| step_order | step | users | step_conv_pct | conv_from_signup_pct |
|---:|---|---:|---:|---:|
| 1 | signup | 12000 | — | — |
| 2 | activation | 9623 | 80.19 | 80.19 |
| 3 | paid_subscription | 3384 | 35.17 | 28.20 |

**Инсайт.** Вход в воронку почти не течёт: `signup → activation` проходит **80.19%** (**9623** из **12000**), и здесь теряется лишь **2377** пользователей. Почти все потери сосредоточены на целевом шаге: `activation → paid_subscription` проходит только **35.17%** (**3384** из **9623**), то есть из **8616** неплативших **6239** отваливаются ровно на конверсии в оплату. Итоговая конверсия от signup — **28.20%**. Это подтверждает приоритет из EDA: рычаг роста выручки лежит между активацией и первым платежом, и именно туда нацелен тестируемый онбординг-флоу.

---

## Q3. Каналы: `referral` конвертит лучше всех, но `organic` приносит больше всего денег

**Бизнес-вопрос:** какой канал привлечения даёт лучшую платную конверсию и сколько чистой выручки/ARPU он приносит?

```sql
WITH users AS (
    SELECT user_id, channel FROM read_csv_auto('data/raw/users.csv')
),
flags AS (
    SELECT user_id,
           MAX(CASE WHEN event_name = 'activation'        THEN 1 ELSE 0 END) AS activated,
           MAX(CASE WHEN event_name = 'paid_subscription' THEN 1 ELSE 0 END) AS paid
    FROM read_csv_auto('data/raw/events.csv') GROUP BY user_id
),
rev AS (
    SELECT user_id, SUM(amount) AS net_revenue
    FROM read_csv_auto('data/raw/transactions.csv') GROUP BY user_id
)
SELECT
    u.channel,
    COUNT(*)                                                    AS users,
    SUM(f.activated)                                            AS activated,
    SUM(f.paid)                                                 AS paid,
    ROUND(100.0 * SUM(f.paid) / COUNT(*), 2)                    AS paid_conv_pct,
    ROUND(100.0 * SUM(f.paid) / NULLIF(SUM(f.activated), 0), 2) AS paid_given_activated_pct,
    ROUND(COALESCE(SUM(r.net_revenue), 0), 2)                   AS net_revenue_rub,
    ROUND(COALESCE(SUM(r.net_revenue), 0) / COUNT(*), 2)        AS arpu_rub,
    ROUND(COALESCE(SUM(r.net_revenue), 0) / NULLIF(SUM(f.paid), 0), 2) AS arppu_rub
FROM users u
LEFT JOIN flags f ON u.user_id = f.user_id
LEFT JOIN rev   r ON u.user_id = r.user_id
GROUP BY 1 ORDER BY net_revenue_rub DESC;
```

| channel | users | activated | paid | paid_conv_pct | paid_given_activated_pct | net_revenue_rub | arpu_rub | arppu_rub |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| organic | 3313 | 2637 | 932 | 28.13 | 35.34 | 3294570 | 994.44 | 3534.95 |
| paid_search | 2652 | 2139 | 684 | 25.79 | 31.98 | 2475530 | 933.46 | 3619.20 |
| social | 2201 | 1762 | 581 | 26.40 | 32.97 | 1988150 | 903.29 | 3421.94 |
| referral | 1480 | 1189 | 498 | 33.65 | 41.88 | 1702520 | 1150.35 | 3418.71 |
| content | 1280 | 1033 | 349 | 27.27 | 33.79 | 1219350 | 952.62 | 3493.84 |
| email | 1074 | 863 | 340 | 31.66 | 39.40 | 1168370 | 1087.87 | 3436.38 |

**Инсайт.** Конверсионный и денежный рейтинги расходятся, и это ключ к решению. По качеству трафика лидирует **`referral`**: **33.65%** платной конверсии и **41.88%** paid given activated при ARPU **1150.35** ₽ — это заметно выше `paid_search` (**25.79%** / **31.98%**, ARPU **933.46** ₽) и `social` (**26.40%** / **32.97%**). Но по абсолютному объёму денег первое место держит **`organic`** — **3294570** ₽ (**27.8%** всей выручки), просто за счёт размера (**3313** пользователей). Интересно, что `arppu_rub` почти одинаков у всех каналов (**3418.71 – 3619.20** ₽): платящий пользователь стоит примерно столько же независимо от источника — канал влияет на **вероятность** оплаты, а не на её величину. Вывод для распределения бюджета: `referral` и `email` (**31.66%**) — самые эффективные по конверсии на привлечённого пользователя, `paid_search` — самый слабый по отдаче на вход.

---

## Q4. Когорты выдыхаются: payer-rate с 32.87% до 19.75%, а ARPU когорты падает в 11 раз

**Бизнес-вопрос:** как выручка и число плательщиков распределяются по когортам (месяц signup)?

```sql
WITH u AS (
    SELECT user_id, date_trunc('month', signup_date) AS cohort_month
    FROM read_csv_auto('data/raw/users.csv')
),
rev AS (
    SELECT user_id, SUM(amount) AS net_revenue, COUNT(*) FILTER (WHERE amount > 0) AS charge_cnt
    FROM read_csv_auto('data/raw/transactions.csv') GROUP BY user_id
)
SELECT
    u.cohort_month,
    COUNT(*)                                              AS signups,
    COUNT(r.user_id)                                      AS payers,
    ROUND(100.0 * COUNT(r.user_id) / COUNT(*), 2)         AS payer_pct,
    COALESCE(SUM(r.charge_cnt), 0)                        AS charges,
    ROUND(COALESCE(SUM(r.net_revenue), 0), 2)             AS net_revenue_rub,
    ROUND(COALESCE(SUM(r.net_revenue), 0) / COUNT(*), 2)  AS arpu_rub
FROM u
LEFT JOIN rev r ON u.user_id = r.user_id
GROUP BY 1 ORDER BY 1;
```

| cohort_month | signups | payers | payer_pct | charges | net_revenue_rub | arpu_rub |
|---|---:|---:|---:|---:|---:|---:|
| 2024-01 | 1091 | 292 | 26.76 | 2146 | 1564520 | 1434.02 |
| 2024-02 | 901 | 243 | 26.97 | 1765 | 1281990 | 1422.85 |
| 2024-03 | 1354 | 445 | 32.87 | 2896 | 2202520 | 1626.68 |
| 2024-04 | 1198 | 383 | 31.97 | 2252 | 1559290 | 1301.58 |
| 2024-05 | 935 | 280 | 29.95 | 1599 | 1195740 | 1278.87 |
| 2024-06 | 842 | 253 | 30.05 | 1246 | 900100 | 1069.00 |
| 2024-07 | 813 | 229 | 28.17 | 1017 | 779540 | 958.84 |
| 2024-08 | 917 | 259 | 28.24 | 970 | 752540 | 820.65 |
| 2024-09 | 1042 | 271 | 26.01 | 842 | 624050 | 598.90 |
| 2024-10 | 1012 | 298 | 29.45 | 728 | 549350 | 542.84 |
| 2024-11 | 953 | 245 | 25.71 | 397 | 303210 | 318.16 |
| 2024-12 | 942 | 186 | 19.75 | 186 | 135640 | 143.99 |

**Инсайт.** Две вещи видны сразу. Во-первых, **мартовская когорта — аномально сильная**: **32.87%** payer-rate и **1626.68** ₽ ARPU против **26.76%** / **1434.02** ₽ у январской; это ровно окно A/B-кампании (`2024-03-01 … 2024-04-30`), и эффект лежит не только в эксперименте, но и в составе притока. Во-вторых, **ARPU когорты монотонно падает** с **1626.68** ₽ (март) до **143.99** ₽ (декабрь) — но это в первую очередь **правое цензурирование**, а не деградация: декабрьская когорта (**942** signup) физически не успевает набрать продления до `2024-12-31`, её `charges` — всего **186** против **2896** у мартовской. Payer-rate декабрьской когорты (**19.75%**) тоже занижен по той же причине. Для сравнения «здоровья» когорт нужны равноудалённые окна наблюдения — эта нога их не строит (это `cohorts`/`ltv`), но именно она фиксирует, что сырой срез по signup-месяцу смешивает эффект кампании и цензурирование.

---

## Q5. Возвраты растут с ценой тарифа: `pro` 3.55% против `basic` 2.33%

**Бизнес-вопрос:** на каком тарифе выше доля возвратов — где качество выручки хуже?

```sql
SELECT
    plan,
    COUNT(*)                                                       AS txn_cnt,
    COUNT(*) FILTER (WHERE is_refund)                              AS refund_cnt,
    ROUND(100.0 * COUNT(*) FILTER (WHERE is_refund) / COUNT(*), 2) AS refund_rate_pct,
    ROUND(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 2)     AS gross_revenue_rub,
    ROUND(-SUM(CASE WHEN amount < 0 THEN amount ELSE 0 END), 2)    AS refunds_rub,
    ROUND(100.0 * -SUM(CASE WHEN amount < 0 THEN amount ELSE 0 END)
          / SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 2)   AS refund_value_pct
FROM read_csv_auto('data/raw/transactions.csv')
GROUP BY 1 ORDER BY refund_rate_pct DESC;
```

| plan | txn_cnt | refund_cnt | refund_rate_pct | gross_revenue_rub | refunds_rub | refund_value_pct |
|---|---:|---:|---:|---:|---:|---:|
| pro | 2365 | 84 | 3.55 | 3626790 | 133560 | 3.68 |
| plus | 4716 | 130 | 2.76 | 4081540 | 115700 | 2.83 |
| basic | 9396 | 219 | 2.33 | 4496730 | 107310 | 2.39 |

**Инсайт.** Доля возвратов монотонно растёт с ценой тарифа: **3.55%** на `pro` против **2.76%** на `plus` и **2.33%** на `basic` — и по деньгам тот же порядок (**3.68%** против **2.83%** и **2.39%**). При этом абсолютное число возвратов больше всего у `basic` (**219** из **433**, то есть **50.6%** всех возвратов) просто потому, что `basic` — самый массовый тариф (**9396** из **16477** транзакций, **57.0%**). Практический вывод: на дорогих тарифах пользователи чаще просят возврат, что логично для более высокого ценового барьера; продуктовой команде стоит проверить, не связан ли рост доли на `pro` с ожиданиями от премиум-функций. Хотя это самая крупная выручка на транзакцию (**3626790** ₽ gross при **2365** транзакциях → **1533.6** ₽ за транзакцию), именно там возвраты «съедают» **133560** ₽.

---

## Сводка по ноге

| # | Вопрос | Главное число | Ответ |
|---|---|---|---|
| 1 | Месячная выручка и возвраты | net **11848490** ₽, возвраты **2.63%** | выручка ×10.28 за год, качество денег стабильно |
| 2 | Шаги воронки и конверсия | `activation → paid` **35.17%** | 72% потерь — на целевом шаге |
| 3 | Канал × конверсия | `referral` **33.65%** / `organic` **3294570** ₽ | конверсия и деньги расходятся |
| 4 | Когорты × выручка | payer-rate **19.75% – 32.87%** | март-буст + декабрьское цензурирование |
| 5 | Возвраты по тарифу | `pro` **3.55%** vs `basic` **2.33%** | возвраты растут с ценой |

---

## Как воспроизвести

```bash
# из корня репозитория: исполняет sql/01_sql.sql (5 запросов), печатает каждый
# результат и перезаписывает sql/results/01_*.csv
uv run --project "/Users/nikitaboarkin/Desktop/00 obsidian/obsidian" python sql/run_01_sql.py
```

Скрипт делает `chdir` в корень проекта, поэтому относительные пути
`read_csv_auto('data/raw/*.csv')` внутри `sql/01_sql.sql` разрешаются к сырым CSV.
Запросы можно также выполнять по одному: DuckDB автоматически парсит
`transaction_date` как `DATE`, а `is_refund` — как `BOOLEAN` (`'True'`/`'False'`).

**Артефакты:**
- `sql/01_sql.sql` — 5 документированных запросов (по одному бизнес-вопросу над каждым);
- `sql/results/01_1_monthly_net_revenue_refund_rate.csv` — 12 строк;
- `sql/results/01_2_funnel_stages_and_step_conversion.csv` — 3 строки;
- `sql/results/01_3_channel_paid_conversion_ranking.csv` — 6 строк;
- `sql/results/01_4_signup_cohort_revenue_and_payers.csv` — 12 строк;
- `sql/results/01_5_refund_rate_by_plan.csv` — 3 строки.

**Контроль сходимости (все запросы дают одно и то же):** gross **12205060** ₽, возвраты **356570** ₽, net **11848490** ₽, **16477** транзакций, **433** возврата, **12000** пользователей, **3384** плательщика — совпадает с `docs/insights/00-eda.md` до рубля.
