-- ============================================================================
-- 02_cohorts.sql — когортная retention-матрица (DuckDB)
-- ============================================================================
-- Нога: `cohorts` (Leg 02).
--
-- Определения
--   * Когорта = календарный месяц регистрации, берётся из `users.signup_date`
--     (12 месячных когорт: 2024-01 … 2024-12).
--   * Индекс месяца M = число месяцев между месяцем регистрации и месяцем
--     события: M0 — месяц регистрации, M1 — следующий месяц и т.д.
--   * Удержание в ячейке (когорта, M) = активные пользователи когорты в этом
--     месяце / размер когорты, в процентах.
--
-- Два варианта активности
--   A) activity retention — «любое событие в месяце» (весь events-лог);
--   B) paid retention     — «платёж в месяце»: `paid_subscription` (первый
--      платёж) или `renew` (продление). Это денежное удержание.
--
-- ВАЖНО (right-censoring): данные заканчиваются 2024-12-31. Поздние когорты
-- физически не могут дожить до высоких M, поэтому ячейки за пределами окна
-- наблюдения остаются NULL (пусто), а НЕ нулём. Так, у когорты 2024-12
-- наблюдаем только M0, у 2024-01 — M0…M11; M12 недостижим ни для одной
-- когорты (для этого нужен 13-й месяц наблюдения).
--
-- ЧТЕНИЕ ФАЙЛОВ: пути относительные, от корня репозитория. Запускать из корня
--   (скрипт sql/run_02_cohorts.py сам делает chdir в корень проекта).
-- ============================================================================


-- @QUERY: activity_retention
-- ----------------------------------------------------------------------------
-- Вариант A: удержание по «любой активности» (любое событие в месяце).
-- Возвращает матрицу: строки = месяц когорты, колонки M0…M11 = retention %.
-- ----------------------------------------------------------------------------
WITH
users AS (
    SELECT
        user_id,
        CAST(signup_date AS DATE)                      AS signup_date,
        date_trunc('month', CAST(signup_date AS DATE)) AS cohort_month
    FROM read_csv_auto('data/raw/users.csv')
),

events AS (
    SELECT
        user_id,
        CAST(event_ts AS TIMESTAMP)                      AS event_ts,
        event_name,
        date_trunc('month', CAST(event_ts AS TIMESTAMP)) AS event_month
    FROM read_csv_auto('data/raw/events.csv')
),

-- размер каждой когорты = число зарегистрированных в этом месяце
cohort_size AS (
    SELECT cohort_month, COUNT(DISTINCT user_id) AS n_users
    FROM users
    GROUP BY cohort_month
),

-- 1 строка = 1 «пользователь активен в месяце M своей когорты»
activity AS (
    SELECT DISTINCT
        u.cohort_month,
        u.user_id,
        date_diff('month', u.cohort_month, e.event_month) AS month_index
    FROM users u
    JOIN events e USING (user_id)
    WHERE date_diff('month', u.cohort_month, e.event_month) BETWEEN 0 AND 11
),

active_counts AS (
    SELECT cohort_month, month_index, COUNT(DISTINCT user_id) AS active_users
    FROM activity
    GROUP BY cohort_month, month_index
)

SELECT
    strftime(c.cohort_month, '%Y-%m') AS cohort_month,
    c.n_users                         AS cohort_size,
    ROUND(100.0 * MAX(CASE WHEN a.month_index =  0 THEN a.active_users END) / c.n_users, 2) AS M0,
    ROUND(100.0 * MAX(CASE WHEN a.month_index =  1 THEN a.active_users END) / c.n_users, 2) AS M1,
    ROUND(100.0 * MAX(CASE WHEN a.month_index =  2 THEN a.active_users END) / c.n_users, 2) AS M2,
    ROUND(100.0 * MAX(CASE WHEN a.month_index =  3 THEN a.active_users END) / c.n_users, 2) AS M3,
    ROUND(100.0 * MAX(CASE WHEN a.month_index =  4 THEN a.active_users END) / c.n_users, 2) AS M4,
    ROUND(100.0 * MAX(CASE WHEN a.month_index =  5 THEN a.active_users END) / c.n_users, 2) AS M5,
    ROUND(100.0 * MAX(CASE WHEN a.month_index =  6 THEN a.active_users END) / c.n_users, 2) AS M6,
    ROUND(100.0 * MAX(CASE WHEN a.month_index =  7 THEN a.active_users END) / c.n_users, 2) AS M7,
    ROUND(100.0 * MAX(CASE WHEN a.month_index =  8 THEN a.active_users END) / c.n_users, 2) AS M8,
    ROUND(100.0 * MAX(CASE WHEN a.month_index =  9 THEN a.active_users END) / c.n_users, 2) AS M9,
    ROUND(100.0 * MAX(CASE WHEN a.month_index = 10 THEN a.active_users END) / c.n_users, 2) AS M10,
    ROUND(100.0 * MAX(CASE WHEN a.month_index = 11 THEN a.active_users END) / c.n_users, 2) AS M11
FROM cohort_size c
LEFT JOIN active_counts a USING (cohort_month)
GROUP BY c.cohort_month, c.n_users
ORDER BY c.cohort_month;


-- @QUERY: paid_retention
-- ----------------------------------------------------------------------------
-- Вариант B: «платёжное» удержание — событие paid_subscription или renew.
-- Когорта по-прежнему = месяц регистрации; M0 = доля, оплатившая в месяц
-- регистрации (первый платёж обычно приходит сразу после активации).
-- ----------------------------------------------------------------------------
WITH
users AS (
    SELECT
        user_id,
        CAST(signup_date AS DATE)                      AS signup_date,
        date_trunc('month', CAST(signup_date AS DATE)) AS cohort_month
    FROM read_csv_auto('data/raw/users.csv')
),

events AS (
    SELECT
        user_id,
        CAST(event_ts AS TIMESTAMP)                      AS event_ts,
        event_name,
        date_trunc('month', CAST(event_ts AS TIMESTAMP)) AS event_month
    FROM read_csv_auto('data/raw/events.csv')
),

cohort_size AS (
    SELECT cohort_month, COUNT(DISTINCT user_id) AS n_users
    FROM users
    GROUP BY cohort_month
),

-- 1 строка = 1 «пользователь оплатил в месяце M своей когорты»
paid_activity AS (
    SELECT DISTINCT
        u.cohort_month,
        u.user_id,
        date_diff('month', u.cohort_month, e.event_month) AS month_index
    FROM users u
    JOIN events e USING (user_id)
    WHERE e.event_name IN ('paid_subscription', 'renew')
      AND date_diff('month', u.cohort_month, e.event_month) BETWEEN 0 AND 11
),

paid_counts AS (
    SELECT cohort_month, month_index, COUNT(DISTINCT user_id) AS active_users
    FROM paid_activity
    GROUP BY cohort_month, month_index
)

SELECT
    strftime(c.cohort_month, '%Y-%m') AS cohort_month,
    c.n_users                         AS cohort_size,
    ROUND(100.0 * MAX(CASE WHEN a.month_index =  0 THEN a.active_users END) / c.n_users, 2) AS M0,
    ROUND(100.0 * MAX(CASE WHEN a.month_index =  1 THEN a.active_users END) / c.n_users, 2) AS M1,
    ROUND(100.0 * MAX(CASE WHEN a.month_index =  2 THEN a.active_users END) / c.n_users, 2) AS M2,
    ROUND(100.0 * MAX(CASE WHEN a.month_index =  3 THEN a.active_users END) / c.n_users, 2) AS M3,
    ROUND(100.0 * MAX(CASE WHEN a.month_index =  4 THEN a.active_users END) / c.n_users, 2) AS M4,
    ROUND(100.0 * MAX(CASE WHEN a.month_index =  5 THEN a.active_users END) / c.n_users, 2) AS M5,
    ROUND(100.0 * MAX(CASE WHEN a.month_index =  6 THEN a.active_users END) / c.n_users, 2) AS M6,
    ROUND(100.0 * MAX(CASE WHEN a.month_index =  7 THEN a.active_users END) / c.n_users, 2) AS M7,
    ROUND(100.0 * MAX(CASE WHEN a.month_index =  8 THEN a.active_users END) / c.n_users, 2) AS M8,
    ROUND(100.0 * MAX(CASE WHEN a.month_index =  9 THEN a.active_users END) / c.n_users, 2) AS M9,
    ROUND(100.0 * MAX(CASE WHEN a.month_index = 10 THEN a.active_users END) / c.n_users, 2) AS M10,
    ROUND(100.0 * MAX(CASE WHEN a.month_index = 11 THEN a.active_users END) / c.n_users, 2) AS M11
FROM cohort_size c
LEFT JOIN paid_counts a USING (cohort_month)
GROUP BY c.cohort_month, c.n_users
ORDER BY c.cohort_month;
