-- =============================================================================
-- 01_sql.sql — Лег `sql` (Leg 01) проекта product-ab-funnel-analytics
-- =============================================================================
-- Набор чистых аналитических срезов на DuckDB поверх сырых CSV из data/raw/.
-- Каждый запрос отвечает на один бизнес-вопрос из project_brief.md и может быть
-- выполнен независимо. Файл исполняется целиком скриптом sql/run_01_sql.py,
-- который печатает каждый результат и экспортирует его в sql/results/01_*.csv.
--
-- ГРАНУЛЯРНОСТЬ (важно, чтобы не поймать fan-out):
--   users.csv        — 1 строка = 1 пользователь (PK user_id);
--   events.csv       — 1 строка = 1 событие;
--   transactions.csv — 1 строка = 1 списание/возврат (PK transaction_id).
--   `events` и `transactions` НЕ соединяем напрямую: обе таблицы
--   присоединяются к users side-by-side (см. 00-eda.md: наивный
--   events JOIN transactions ON user_id даёт 156223 строки вместо 38892, ×4.02).
--
-- ЧТЕНИЕ ФАЙЛОВ: пути относительные, от корня репозитория. Запускать из корня
--   (скрипт сам делает chdir в корень проекта).
--
-- ЗАМЕТКА ПО ДЕНЬГАМ: `amount` возвратов уже отрицательный, поэтому SUM(amount)
--   сразу даёт net revenue; gross считаем через amount > 0.
-- =============================================================================


-- === QUERY 1: monthly_net_revenue_refund_rate ===
-- Бизнес-вопрос: как меняется чистая выручка по месяцам и не растёт ли доля
-- возвратов со временем (то есть не портится ли качество выручки к концу года)?
SELECT
    date_trunc('month', transaction_date)                        AS month,
    ROUND(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 2)   AS gross_revenue_rub,
    ROUND(-SUM(CASE WHEN amount < 0 THEN amount ELSE 0 END), 2)  AS refunds_rub,
    ROUND(SUM(amount), 2)                                        AS net_revenue_rub,
    COUNT(*)                                                     AS txn_cnt,
    COUNT(*) FILTER (WHERE is_refund)                            AS refund_cnt,
    ROUND(100.0 * COUNT(*) FILTER (WHERE is_refund) / COUNT(*), 2) AS refund_rate_pct
FROM read_csv_auto('data/raw/transactions.csv')
GROUP BY 1
ORDER BY 1;


-- === QUERY 2: funnel_stages_and_step_conversion ===
-- Бизнес-вопрос: сколько уникальных пользователей проходит каждый шаг воронки
-- signup → activation → paid_subscription и какая конверсия на каждом переходе?
-- (Шаги берём как DISTINCT user_id по event_name; биллинговые renew/cancel
--  в шаги воронки не входят, иначе шаги перестают быть монотонными.)
WITH s AS (
    SELECT
        COUNT(DISTINCT user_id) FILTER (WHERE event_name = 'signup')            AS signup_users,
        COUNT(DISTINCT user_id) FILTER (WHERE event_name = 'activation')        AS activation_users,
        COUNT(DISTINCT user_id) FILTER (WHERE event_name = 'paid_subscription') AS paid_users
    FROM read_csv_auto('data/raw/events.csv')
)
SELECT * FROM (
    SELECT 1 AS step_order, 'signup'            AS step, signup_users     AS users,
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


-- === QUERY 3: channel_paid_conversion_ranking ===
-- Бизнес-вопрос: какой канал привлечения даёт лучшую платную конверсию и
-- сколько чистой выручки/ARPU он приносит — куда стоит переносить бюджет?
-- (grain: users как центр; flags и rev агрегированы до user_id ДО джойна.)
WITH users AS (
    SELECT user_id, channel
    FROM read_csv_auto('data/raw/users.csv')
),
flags AS (
    SELECT user_id,
           MAX(CASE WHEN event_name = 'activation'        THEN 1 ELSE 0 END) AS activated,
           MAX(CASE WHEN event_name = 'paid_subscription' THEN 1 ELSE 0 END) AS paid
    FROM read_csv_auto('data/raw/events.csv')
    GROUP BY user_id
),
rev AS (
    SELECT user_id, SUM(amount) AS net_revenue
    FROM read_csv_auto('data/raw/transactions.csv')
    GROUP BY user_id
)
SELECT
    u.channel,
    COUNT(*)                                                          AS users,
    SUM(f.activated)                                                  AS activated,
    SUM(f.paid)                                                       AS paid,
    ROUND(100.0 * SUM(f.paid) / COUNT(*), 2)                          AS paid_conv_pct,
    ROUND(100.0 * SUM(f.paid) / NULLIF(SUM(f.activated), 0), 2)       AS paid_given_activated_pct,
    ROUND(COALESCE(SUM(r.net_revenue), 0), 2)                         AS net_revenue_rub,
    ROUND(COALESCE(SUM(r.net_revenue), 0) / COUNT(*), 2)              AS arpu_rub,
    ROUND(COALESCE(SUM(r.net_revenue), 0) / NULLIF(SUM(f.paid), 0), 2) AS arppu_rub
FROM users u
LEFT JOIN flags f ON u.user_id = f.user_id
LEFT JOIN rev   r ON u.user_id = r.user_id
GROUP BY 1
ORDER BY net_revenue_rub DESC;


-- === QUERY 4: signup_cohort_revenue_and_payers ===
-- Бизнес-вопрос: как выручка и число плательщиков распределяются по когортам
-- (месяц signup) — какие когорты самые «денежные» и где видно цензурирование?
-- (grain: user × signup-month; декабрьская когорта неполная — окно кончается 31.12.)
WITH u AS (
    SELECT user_id, date_trunc('month', signup_date) AS cohort_month
    FROM read_csv_auto('data/raw/users.csv')
),
rev AS (
    SELECT user_id,
           SUM(amount)                              AS net_revenue,
           COUNT(*) FILTER (WHERE amount > 0)       AS charge_cnt
    FROM read_csv_auto('data/raw/transactions.csv')
    GROUP BY user_id
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
GROUP BY 1
ORDER BY 1;


-- === QUERY 5: refund_rate_by_plan ===
-- Бизнес-вопрос: на каком тарифе выше доля возвратов — где качество выручки
-- хуже и куда смотреть продуктовой команде?
SELECT
    plan,
    COUNT(*)                                                        AS txn_cnt,
    COUNT(*) FILTER (WHERE is_refund)                               AS refund_cnt,
    ROUND(100.0 * COUNT(*) FILTER (WHERE is_refund) / COUNT(*), 2)  AS refund_rate_pct,
    ROUND(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 2)      AS gross_revenue_rub,
    ROUND(-SUM(CASE WHEN amount < 0 THEN amount ELSE 0 END), 2)     AS refunds_rub,
    ROUND(100.0 * -SUM(CASE WHEN amount < 0 THEN amount ELSE 0 END)
          / SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 2)    AS refund_value_pct
FROM read_csv_auto('data/raw/transactions.csv')
GROUP BY 1
ORDER BY refund_rate_pct DESC;
