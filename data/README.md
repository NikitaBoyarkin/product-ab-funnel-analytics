# Data — provenance & schema

> ## ⚠️ SYNTHETIC — NOT REAL DATA
> Every row in `data/raw/` was **fabricated** by `data/raw/_generate.py`.
> There are no real users, payments, or personal data here. Numbers are
> plausible-looking inventions, not measurements. Do not cite as evidence
> about any real product.

## 1. Source

| Field | Value |
|---|---|
| Source | `synthetic` (no external dataset) |
| Generator | `data/raw/_generate.py` (stdlib + numpy + pandas only) |
| Seed | **42** (`numpy.random.default_rng(42)`) |
| Reproducible | Yes — byte-identical CSVs across re-runs (verified: identical SHA-256 after two runs) |
| Regenerate | `uv run --project <env> python data/raw/_generate.py` |
| Period | `2024-01-01` … `2024-12-31` (366 days, leap year) |
| Currency | RUB |
| Language of the domain | ru (subscription B2C product) |

Regenerate with any Python env that has `numpy` + `pandas`:

```bash
python data/raw/_generate.py
```

## 2. Files (row counts read from disk)

| File | Rows | Columns |
|---|---|---|
| `data/raw/events.csv` | **38,892** (+1 header = 38,893 lines) | 8 |
| `data/raw/transactions.csv` | **16,477** (+1 header = 16,478 lines) | 6 |
| `data/raw/users.csv` | **12,000** (+1 header = 12,001 lines) | 7 |

### `events.csv` — product event log

Columns: `user_id, event_ts, event_name, platform, country, channel, experiment_group, experiment_flag`

`event_name ∈ {signup, activation, paid_subscription, renew, cancel}` — a funnel plus monthly billing:

| event_name | rows | meaning |
|---|---:|---|
| `signup` | 12,000 | account created (one per user) |
| `activation` | 9,623 | onboarding completed (80.2% of signups) |
| `paid_subscription` | 3,384 | first paid charge (35.2% of *activated* users) |
| `renew` | 12,660 | recurring monthly charge |
| `cancel` | 1,225 | subscription lapsed (failed to renew) |

- `event_ts`: `YYYY-MM-DD HH:MM:SS`; range `2024-01-01 00:06:13` … `2024-12-31 23:55:42`.
- `experiment_group` ∈ `{control, treatment, none}`. Populated for the **whole row set of a user** whose `signup_date` falls inside the A/B window `2024-03-01 … 2024-04-30`; `none` otherwise. Assignment is 50/50 per user and constant across that user's events.
- `experiment_flag`: `1` for experiment-cohort users, else `0` (same row scope as `experiment_group`).
- No nulls; every `user_id` appears in `users.csv`.
- Rows are sorted by timestamp, then `user_id`, then funnel stage.

### `transactions.csv` — payment ledger (paying users only)

Columns: `user_id, transaction_id, transaction_date, amount, plan, is_refund`

- **16,044 charges** + **433 refunds** (2.63% of all transactions = 433/16,477; 2.70% of charges).
- `is_refund == True` rows carry a **negative** `amount` (money returned), so `amount.sum()` is already net.
- `plan` ∈ `{basic, plus, pro}` → flat monthly price `490 / 890 / 1590` RUB.
- **AOV** (charges): mean **760.7** RUB, median **490** RUB.
- Gross charges **12,205,060** RUB; refunds **−356,570** RUB; **net revenue 11,848,490** RUB.
- Charges per paying user: **4.74** (first payment + monthly renewals).
- `transaction_date`: `YYYY-MM-DD`; range `2024-01-05` … `2024-12-31`.

### `users.csv` — acquired-user cohorts

Columns: `user_id, signup_date, country, channel, platform, experiment_group, acquisition_cost`

- 12,000 users, `signup_date` `2024-01-01` … `2024-12-31` (seasonal, with a Mar–Apr campaign bump).
- `country`: RU 9,350 · KZ 1,073 · BY 716 · AM 483 · Other 378.
- `channel`: organic 3,313 · paid_search 2,652 · social 2,201 · referral 1,480 · content 1,280 · email 1,074.
- `platform`: android 5,251 · ios 4,071 · web 2,678.
- `experiment_group`: none 9,448 · control 1,271 · treatment 1,281.
- `acquisition_cost` (CAC, RUB): mean **598.5**, min 400, max 900.

## 3. What the generator baked in (so each leg finds something)

| Mechanism | Value |
|---|---|
| Signup → activation | ~80% (platform-dependent: ios > android > web) |
| Activation → paid (baseline) | 35% (channel-dependent intent multiplier) |
| **A/B effect on activation → paid** | **+25% relative baked in** (control 35% → treatment 43.75%); realized on the seed: **+19.6% relative** |
| Retention | power-law survival `S(t) = 0.08 + 0.92·(1 + t/30)^(-0.30)`, monthly billing → a **dropping, flattening** curve |
| AOV | mean ~761 RUB (plan mix basic/plus/pro) |
| CAC | 400–900 RUB by channel |

Retention shape (illustrative, `2024-01` paid cohort, share still paying in month *m*):
`1.000 → 0.774 → 0.729 → 0.656 → 0.593 → 0.552 → 0.511 → 0.480 → 0.480 → 0.462 → 0.462 → 0.448`.
This is the curve the `ltv` leg should fit as a power law.

### A/B experiment facts (for the `a/b` leg)

- Cohort: **2,552** users (control 1,271 / treatment 1,281 → ratio 0.992).
- **SRM check: χ² = 0.039** vs 50/50 (critical @ df=1, α=0.01 → 6.63) → **no SRM**.
- Target metric activation → paid: control **36.11%**, treatment **43.19%**.
- Lift **+19.6% relative** (+7.07 pp), **z = 3.31, p ≈ 9.2e-4** → significant.

## 4. Assumptions & limitations (explicit)

- **Fully synthetic.** Distributions, funnel rates, retention, and the experiment effect are chosen for realism + analysability, not measured from any product.
- The A/B effect is **planted by construction** — a significant result is expected and is *not* evidence about anything real.
- `amount` is a flat price per plan (no discounts, taxes, FX, or price changes over the year).
- Refunds are independent noise (~3% of charges) and **do not** alter the subscription lifecycle.
- Monthly renewal cadence only (≤12 renewals per user per year).
- Late-December signups are right-censored: a conversion/renewal beyond `2024-12-31` simply does not appear.
- Dates are stored as `str` and `user_id` as `str` (`u_000001`); cast on read for analysis.
- **Event volume note:** the brief suggested ~120k–200k event rows. That is mathematically incompatible with the specified 8k–12k users **and** 15k–25k transactions under a realistic monthly-renewal model — total events ≈ users + activations + payers + renewals + cancels, and renewals can never exceed ~12/user/year. The delivered **38,892** events sit at the realistic ceiling for these cohort/ledger sizes. All five event types and every analysis leg are fully supported.

## 5. License / usage

Synthetic sample data — no real individuals. Free to reuse, modify, and redistribute as sample data. The generator script is part of this repository and covered by the repository's license.

## 6. Integrity

SHA-256 of the committed CSVs (produced by `_generate.py`, seed 42):

```
17d47619694bca7ba19dfb929193cdbb2930643daec1eb9f61a49d01e6906f26  events.csv
547def6623798deb57069e97b0ca0b5906d77c92f07be1bbc63b5e7cdc1fd02c  transactions.csv
e9e75a24a3832b012dfead4838f55e40c63c41bb2d31c664df10e5c9f0777465  users.csv
```
