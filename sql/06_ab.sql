-- =============================================================================
-- 06_ab.sql — нога `a/b`: счётчики воронки по группам эксперимента
-- -----------------------------------------------------------------------------
-- Что считает:
--   * когорта A/B = пользователи с experiment_group IN ('control','treatment')
--     (окно 2024-03-01 … 2024-04-30 совпадает с users.signup_date до дня);
--   * PRIMARY-метрику  paid given activated = paid / activated;
--   * SECONDARY-метрику activation rate     = activated / users;
--   * guardrail-счётчики: возвраты (refunds) и отмены (cancel) по группам.
--
-- Контракт grain'ов (из 00-eda.md): считаем на grain `user`, `events` фильтруем
--   по event_name до агрегации, `transactions` сворачиваем до user_id ДО сшивки
--   (прямой events JOIN transactions даёт ×4.02 fan-out и задваивает выручку).
--
-- Запуск (DuckDB, 1.5+):
--   duckdb -c ".read sql/06_ab.sql"
--   либо из раннера analysis/run_06_ab.py (он же пишет sql/results/06_ab_counts.csv)
-- =============================================================================

WITH cohort AS (
    -- 1 строка = 1 пользователь A/B-когорты; groups сбалансированы (SRM проверяется в раннере)
    SELECT
        user_id,
        experiment_group AS grp
    FROM read_csv_auto('data/raw/users.csv', header = true)
    WHERE experiment_group IN ('control', 'treatment')
),

events AS (
    SELECT user_id, event_name
    FROM read_csv_auto('data/raw/events.csv', header = true)
),

-- 1 строка = 1 пользователь: бинарные флаги шагов воронки (фильтр по event_name внутри MAX)
user_flags AS (
    SELECT
        c.user_id,
        c.grp,
        MAX(CASE WHEN e.event_name = 'activation'        THEN 1 ELSE 0 END) AS activated,
        MAX(CASE WHEN e.event_name = 'paid_subscription' THEN 1 ELSE 0 END) AS paid,
        MAX(CASE WHEN e.event_name = 'cancel'            THEN 1 ELSE 0 END) AS cancelled
    FROM cohort c
    LEFT JOIN events e USING (user_id)
    GROUP BY c.user_id, c.grp
),

-- 1 строка = 1 пользователь с транзакциями: списания и возвраты (is_refund — BOOLEAN)
txn AS (
    SELECT
        user_id,
        COUNT(*) FILTER (WHERE is_refund = FALSE) AS charges,
        COUNT(*) FILTER (WHERE is_refund = TRUE)  AS refunds
    FROM read_csv_auto('data/raw/transactions.csv', header = true)
    GROUP BY user_id
),

-- сшиваем флаги воронки с денежными счётчиками на grain user (LEFT JOIN — неплательщики дают 0)
user_agg AS (
    SELECT
        uf.grp,
        uf.activated,
        uf.paid,
        uf.cancelled,
        COALESCE(t.charges, 0) AS charges,
        COALESCE(t.refunds, 0) AS refunds
    FROM user_flags uf
    LEFT JOIN txn t USING (user_id)
)

-- Итоговая таблица: 1 строка = 1 группа; numerator/denominator явно поименованы
SELECT
    grp AS experiment_group,

    -- размер группы
    COUNT(*)                                              AS users,

    -- PRIMARY: paid given activated  (знаменатель — активировавшиеся, не вся группа)
    SUM(paid)                                             AS paid_given_activated_num,
    SUM(activated)                                        AS paid_given_activated_den,

    -- SECONDARY: activation rate (signup -> activation)
    SUM(activated)                                        AS activation_num,
    COUNT(*)                                              AS activation_den,

    -- вспомогательное: paid per signup (сквозная конверсия группы)
    SUM(paid)                                             AS paid_per_signup_num,

    -- guardrail 1: возвраты
    SUM(charges)                                          AS charges,
    SUM(refunds)                                          AS refunds,
    SUM(CASE WHEN refunds > 0 THEN 1 ELSE 0 END)          AS payers_with_refund,
    SUM(paid)                                             AS payers,

    -- guardrail 2: отмены
    SUM(cancelled)                                        AS cancelled
FROM user_agg
GROUP BY grp
ORDER BY grp;
