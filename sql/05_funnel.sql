-- =====================================================================
-- 05_funnel.sql — воронка активации (нога `funnel`, Leg 05)
-- Движок: DuckDB. Запуск из корня проекта:
--     cd <project> && duckdb < sql/05_funnel.sql
-- Источники: data/raw/events.csv (лог событий), data/raw/users.csv (атрибуты пользователя).
--
-- ЗАЧЕМ. Считаем число УНИКАЛЬНЫХ пользователей, дошедших до каждого шага воронки,
-- в разрезах: overall + channel + platform + country + experiment_group.
--
-- ПРАВИЛА (зафиксированы, чтобы шаги были монотонными):
--   1) Grain — пользователь, не событие. `renew` встречается 12 660 раз на 2 588
--      пользователей, поэтому наивный COUNT(*) по event_name раздувает шаг и ломает
--      монотонность. Считаем COUNT(DISTINCT user_id) через MIN(ts) по каждому шагу.
--   2) Порядок шагов строгий: signup → activation → paid_subscription → renew.
--      Пользователь засчитывается на шаге k только если у него есть событие шага k
--      И его время не раньше времени предыдущего шага (t_k >= t_{k-1}).
--      EDA (00-eda.md) подтвердила 0 нарушений порядка: 0 activation позже paid,
--      0 renew раньше первого paid — правило выполняется механически и ничего не режет.
--   3) `cancel` в шаги воронки НЕ входит: это исход (отток), а не ступень активации.
--   4) `renew` — второй вариант воронки (удержание). Он монотонен: 2 588 <= 3 384.
--
-- ВЫХОД: длинный формат — одна строка на (scope_type, scope_value, step_index, step).
--   scope_type='overall', scope_value='all' — основная воронка (шаги 1..4).
--   Остальные scope_type — сегментные срезы (шаги 1..4 внутри сегмента).
-- =====================================================================

WITH ev AS (
    -- лог событий; event_ts хранится как строка 'YYYY-MM-DD HH:MM:SS' — парсим в TIMESTAMP
    SELECT
        user_id,
        event_name,
        CAST(event_ts AS TIMESTAMP) AS ts
    FROM read_csv_auto('data/raw/events.csv', header = true)
),

firsts AS (
    -- первый таймстамп каждого шага для каждого пользователя (NULL, если шага нет)
    SELECT
        user_id,
        MIN(ts) FILTER (WHERE event_name = 'signup')            AS t_signup,
        MIN(ts) FILTER (WHERE event_name = 'activation')        AS t_activation,
        MIN(ts) FILTER (WHERE event_name = 'paid_subscription') AS t_paid,
        MIN(ts) FILTER (WHERE event_name = 'renew')             AS t_renew
    FROM ev
    GROUP BY user_id
),

reached AS (
    -- булевы флаги «пользователь дошёл до шага» с учётом правила порядка.
    -- NULL-сравнение даёт NULL => условие не выполнено => пользователь не засчитан.
    SELECT
        user_id,
        t_signup IS NOT NULL                                          AS at_signup,
        t_activation IS NOT NULL AND t_activation >= t_signup         AS at_activation,
        t_paid       IS NOT NULL AND t_paid       >= t_activation     AS at_paid,
        t_renew      IS NOT NULL AND t_renew      >= t_paid           AS at_renew
    FROM firsts
),

u AS (
    -- атрибуты сегментации берём из users.csv (источник истины по приобретению)
    SELECT user_id, channel, platform, country, experiment_group
    FROM read_csv_auto('data/raw/users.csv', header = true)
),

j AS (
    -- users LEFT JOIN reached на уровне пользователя (1:1, fan-out невозможен)
    SELECT
        u.user_id, u.channel, u.platform, u.country, u.experiment_group,
        COALESCE(r.at_signup, FALSE)     AS at_signup,
        COALESCE(r.at_activation, FALSE) AS at_activation,
        COALESCE(r.at_paid, FALSE)       AS at_paid,
        COALESCE(r.at_renew, FALSE)      AS at_renew
    FROM u
    LEFT JOIN reached r USING (user_id)
),

scoped AS (
    -- разворачиваем каждый разрез в единый длинный формат (scope_type, scope_value, ...)
    SELECT 'overall' AS scope_type, 'all' AS scope_value,
           at_signup, at_activation, at_paid, at_renew FROM j
    UNION ALL
    SELECT 'channel', channel, at_signup, at_activation, at_paid, at_renew FROM j
    UNION ALL
    SELECT 'platform', platform, at_signup, at_activation, at_paid, at_renew FROM j
    UNION ALL
    SELECT 'country', country, at_signup, at_activation, at_paid, at_renew FROM j
    UNION ALL
    SELECT 'experiment_group', experiment_group, at_signup, at_activation, at_paid, at_renew FROM j
),

step_counts AS (
    -- по одному шагу на строку; COUNT(*) FILTER = число уникальных пользователей,
    -- дошедших до шага (каждая строка `scoped` = ровно один пользователь)
    SELECT scope_type, scope_value, 1 AS step_index, 'signup' AS step,
           COUNT(*) FILTER (WHERE at_signup) AS users
    FROM scoped GROUP BY scope_type, scope_value
    UNION ALL
    SELECT scope_type, scope_value, 2, 'activation',
           COUNT(*) FILTER (WHERE at_signup AND at_activation)
    FROM scoped GROUP BY scope_type, scope_value
    UNION ALL
    SELECT scope_type, scope_value, 3, 'paid_subscription',
           COUNT(*) FILTER (WHERE at_signup AND at_activation AND at_paid)
    FROM scoped GROUP BY scope_type, scope_value
    UNION ALL
    SELECT scope_type, scope_value, 4, 'renew',
           COUNT(*) FILTER (WHERE at_signup AND at_activation AND at_paid AND at_renew)
    FROM scoped GROUP BY scope_type, scope_value
)

SELECT scope_type, scope_value, step_index, step, users
FROM step_counts
ORDER BY scope_type, scope_value, step_index;
