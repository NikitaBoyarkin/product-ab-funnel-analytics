-- =============================================================================
-- 03_rfm.sql — RFM-сегментация (Нога 03: `rfm`)
-- Проект: product-ab-funnel-analytics · Движок: DuckDB
--
-- Назначение: построить базовую RFM-таблицу (recency / frequency / monetary
-- на пользователя), затем присвоить R/F/M-скоры (квартили), собрать RFM-класс
-- и именованные сегменты, и агрегировать их по сегментам.
--
-- Контракт (см. data/README.md и docs/insights/00-eda.md):
--   * grain входа  : 1 строка = 1 транзакция (charge или refund)
--   * grain выхода : 1 строка = 1 плательщик (user_id)
--
-- Правило обработки возвратов (refunds): БЕРЁМ ТОЛЬКО СПИСАНИЯ (amount > 0).
--   Мотивация: возврат — это не покупка. Он не должен увеличивать frequency
--   (число покупок) и не должен «обнулять» monetary у пользователя, который
--   всё же платил. На диске: 16477 транзакций = 16044 списания + 433 возврата
--   (возвраты всегда с отрицательным amount, is_refund = True в 100% строк).
--   Отфильтровано: 433 строки возвратов (2.63%). Пользователей, у которых
--   ТОЛЬКО возвраты и ни одного списания, — 0, поэтому база плательщиков
--   не сужается: 3384 пользователя.
--
-- Дата отсчёта recency (snapshot): 2024-12-31 — максимальная дата транзакции
--   в датасете (совпадает с концом периода анализа). recency = число дней
--   от последней покупки пользователя до 2024-12-31; 0 = купил в последний день.
--
-- Соглашение о скорах (совпадает с каноническим ядром
--   .claude/skills/rfm-segmentation/scripts/rfm.py, bins = 4):
--     R_Score: 4 = недавний,      1 = давний      (меньше recency → больше скор)
--     F_Score: 4 = самый частый,  1 = самый редкий
--     M_Score: 4 = самая большая сумма, 1 = самая маленькая
--     RFMClass = строка из трёх цифр (например '444' — лучший, '111' — худший)
--     RFMScore = R + F + M, диапазон 3..12, больше = лучше
--   Скор считается ПО РАНГАМ (как в Python-ядре _quantile: rank(method='first'),
--   затем ceil(rank / n * bins)), а не NTILE — так SQL воспроизводит ядро
--   бит-в-бит: при равных значениях тай-группа разрезается детерминированно по
--   user_id ASC. Скор = «доля» 1..4 от n, гарантированно по 25% на квартиль.
-- =============================================================================

-- ----------------------------------------------------------------------------
-- Шаг 1. Списанные транзакции (charges). Возвраты исключаем: amount > 0.
-- ----------------------------------------------------------------------------
WITH charges AS (
    SELECT
        user_id,
        CAST(transaction_date AS DATE) AS txn_date,
        amount
    FROM read_csv_auto('data/raw/transactions.csv', header = true)
    WHERE amount > 0
),

-- ----------------------------------------------------------------------------
-- Шаг 2. Базовые RFM-метрики на пользователя.
--   recency   — дней от последней покупки до даты отсчёта 2024-12-31
--   frequency — число списаний (покупок и продлений)
--   monetary  — сумма списаний в ₽ (gross; возвраты уже исключены)
-- ----------------------------------------------------------------------------
rfm_base AS (
    SELECT
        user_id,
        date_diff('day', MAX(txn_date), DATE '2024-12-31') AS recency,
        COUNT(*)                                           AS frequency,
        SUM(amount)                                        AS monetary
    FROM charges
    GROUP BY user_id
),

-- ----------------------------------------------------------------------------
-- Шаг 3. Скоры по рангам (1..4, больше = лучше) — порт _quantile из ядра.
--   ceil(rank / n * 4): rank 1..n/4 → 1, ..., 3n/4+1..n → 4.
--   R инвертируется (5 - score), т.к. меньше recency = лучше.
-- ----------------------------------------------------------------------------
ranked AS (
    SELECT
        *,
        ROW_NUMBER() OVER (ORDER BY recency ASC,   user_id ASC) AS r_rank,
        ROW_NUMBER() OVER (ORDER BY frequency ASC, user_id ASC) AS f_rank,
        ROW_NUMBER() OVER (ORDER BY monetary ASC,  user_id ASC) AS m_rank,
        COUNT(*) OVER ()                                        AS n
    FROM rfm_base
),

scored AS (
    SELECT
        user_id,
        recency,
        frequency,
        monetary,
        5 - CAST(CEIL(r_rank * 4.0 / n) AS INTEGER) AS r_score,  -- недавние → 4
        CAST(CEIL(f_rank * 4.0 / n) AS INTEGER)     AS f_score,  -- частые   → 4
        CAST(CEIL(m_rank * 4.0 / n) AS INTEGER)     AS m_score   -- крупные  → 4
    FROM ranked
),

-- ----------------------------------------------------------------------------
-- Шаг 4. RFM-класс, суммарный скор и именованные сегменты.
--   Пороги повторяют assign_segment из Python-ядра при bins = 4:
--   top = 4, mid = 3, low = 2.
-- ----------------------------------------------------------------------------
segmented AS (
    SELECT
        *,
        r_score::VARCHAR || f_score::VARCHAR || m_score::VARCHAR AS rfm_class,
        r_score + f_score + m_score                             AS rfm_score,
        CASE
            WHEN r_score >= 4 AND f_score >= 4 AND m_score >= 4 THEN 'Champions'
            WHEN r_score >= 3 AND f_score >= 3 AND m_score >= 3 THEN 'Loyal'
            WHEN r_score >= 3 AND f_score >= 2 AND m_score >= 3 THEN 'Potential Loyalists'
            WHEN r_score <= 2 AND f_score >= 3                 THEN 'At Risk'
            WHEN r_score <= 2 AND f_score <= 2                 THEN 'Hibernating'
            ELSE 'Need Attention'
        END AS segment
    FROM scored
)

-- ----------------------------------------------------------------------------
-- Шаг 5. Итог: скоренная таблица на пользователя.
-- ----------------------------------------------------------------------------
SELECT
    user_id,
    recency,
    frequency,
    monetary,
    r_score,
    f_score,
    m_score,
    rfm_class,
    rfm_score,
    segment
FROM segmented
ORDER BY user_id;

-- ----------------------------------------------------------------------------
-- Шаг 6. Сводка по сегментам (раскомментируйте и выполните отдельно).
--   users, доля пользователей, выручка, доля выручки, средние R/F/M.
-- ----------------------------------------------------------------------------
-- SELECT
--     segment,
--     COUNT(*)                                    AS users,
--     ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2)                 AS share_users_pct,
--     SUM(monetary)                               AS revenue,
--     ROUND(100.0 * SUM(monetary) / SUM(SUM(monetary)) OVER (), 2)       AS share_revenue_pct,
--     ROUND(AVG(recency), 2)                      AS avg_recency,
--     ROUND(AVG(frequency), 2)                    AS avg_frequency,
--     ROUND(AVG(monetary), 2)                     AS avg_monetary,
--     ROUND(AVG(rfm_score), 2)                    AS avg_rfm_score
-- FROM segmented
-- GROUP BY segment
-- ORDER BY share_revenue_pct DESC;
