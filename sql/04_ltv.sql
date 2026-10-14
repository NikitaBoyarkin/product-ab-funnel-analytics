-- ============================================================================
-- 04_ltv.sql — входные точки для LTV-ноги (retention-by-period).
-- Движок: DuckDB. Читает data/raw/{transactions,users}.csv напрямую.
--
-- Задача: собрать кривую удержания платящей базы по месяцам жизни подписки.
--
-- Определения (фиксированы для всей ноги):
--   * «платящая когорта» = месяц ПЕРВОГО списания пользователя (pay_month),
--     а не месяц signup: retention считается внутри платящей базы, поэтому
--     t=1 — это месяц первой оплаты, и R(1)=1.0 по построению.
--   * t = номер месяца жизни (1,2,3,…): t = месяцы_от(pay_month → cal_month) + 1.
--   * «активен в месяц m» = есть хотя бы одно НЕвозвратное списание (charge) в m.
--     Возвраты (is_refund=TRUE, отрицательный amount) исключены — они не
--     продлевают подписку и не меняют её жизненный цикл (см. data/README.md §4).
--   * Правое цензурирование: когорта наблюдаема в момент t только если
--     pay_month + (t-1) <= 2024-12. Иначе месяц выходит за окно 2024-12-31,
--     и пользователь физически не мог продлиться. Знаменатель R(t) — только
--     «eligible» когорты, чтобы декабрьская когорта не занижала хвост кривой.
--
-- Выход: t, active_users, eligible_users, retention = active/eligible.
--   Это ровно те точки (t, retention), что скармливаются power-law фиту
--   R(t) = a * t^(-b) в analysis/run_04_ltv.py.
-- ============================================================================

WITH
-- 1) Только списания (charges); возвраты выброшены.
charges AS (
    SELECT
        user_id,
        CAST(transaction_date AS DATE) AS txn_date
    FROM read_csv_auto('data/raw/transactions.csv')
    WHERE is_refund = FALSE
),

-- 2) Первое списание и «платящая когорта» = месяц первой оплаты.
first_pay AS (
    SELECT
        user_id,
        MIN(txn_date)                        AS first_pay_date,
        DATE_TRUNC('month', MIN(txn_date))   AS pay_month
    FROM charges
    GROUP BY user_id
),

-- 3) Размер каждой платящей когорты (знаменатель при полном окне наблюдения).
cohort_size AS (
    SELECT pay_month, COUNT(*) AS cohort_users
    FROM first_pay
    GROUP BY pay_month
),

-- 4) Активные пользователь-месяцы: (user_id, календарный месяц) с >=1 списанием.
user_month AS (
    SELECT
        user_id,
        DATE_TRUNC('month', txn_date) AS cal_month
    FROM charges
    GROUP BY user_id, cal_month
),

-- 5) Индекс t = месяц жизни относительно первой оплаты (+1, чтобы t стартовал с 1).
activity AS (
    SELECT
        um.user_id,
        fp.pay_month,
        um.cal_month,
        DATE_DIFF('month', fp.pay_month, um.cal_month) + 1 AS t
    FROM user_month um
    JOIN first_pay  fp USING (user_id)
),

-- 6) Сетка t = 1..12 (12-месячное окно наблюдения).
t_grid AS (
    SELECT UNNEST(range(1, 13)) AS t
),

-- 7) Знаменатель: сколько пользователей «наблюдаемы» в момент t
--    (когорты, у которых pay_month + (t-1) не выходит за 2024-12).
eligible AS (
    SELECT
        g.t,
        SUM(cs.cohort_users) AS eligible_users
    FROM t_grid g
    JOIN cohort_size cs
      ON cs.pay_month + INTERVAL (g.t - 1) MONTH <= DATE '2024-12-01'
    GROUP BY g.t
),

-- 8) Числитель: сколько уникальных пользователей реально активны в момент t
--    среди наблюдаемых когорт.
active AS (
    SELECT
        a.t,
        COUNT(DISTINCT a.user_id) AS active_users
    FROM activity a
    WHERE a.pay_month + INTERVAL (a.t - 1) MONTH <= DATE '2024-12-01'
    GROUP BY a.t
)

-- 9) Итоговая кривая удержания: (t, retention) — вход для фита.
SELECT
    a.t                                                        AS period,
    a.active_users                                             AS active_users,
    e.eligible_users                                           AS eligible_users,
    ROUND(a.active_users * 1.0 / e.eligible_users, 4)          AS retention
FROM active a
JOIN eligible e USING (t)
ORDER BY a.t;
