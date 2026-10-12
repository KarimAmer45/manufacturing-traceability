# Manufacturing Traceability & Quality Database

Project 1 of the Partec portfolio: trace a customer complaint back to the bath analysis in force, then identify affected lots, customers and deliveries. This folder is a standalone project with its own migrations, dependencies, configuration, tests and startup commands.

**Status:** the simulated production workflow runs end to end and its tests pass locally, and the official Bosch training data is loaded and benchmarked (see below). The Docker/Flyway path also ran cleanly on 2026-10-12; the GitHub Actions workflow is in the repository, but I have not run it yet.

## Demo results

The default run covers 26 weeks from 2026-03-02 with seed 42. I first ran it on 2026-10-04. After adding the hardening migrations V009–V013, I migrated that database and regenerated the files in `docs/results/` on 2026-10-11 with PostgreSQL 17.11 and Python 3.12 (time zone Europe/Berlin); all counts below came out the same.

| Evidence | Actual result |
| --- | ---: |
| Incoming lots | 5,200 |
| Rework children, awaiting processing | 487 |
| Total lots | 5,687 |
| Carrier runs | 11,662 |
| Measurements | 27,733 |
| Daily bath analyses | 130 |
| Derived plating slots | 37 |
| Tests passing | 44 |
| Bad nickel analyses | 2 |
| Affected customers | 5 |
| Shipped lots in containment | 46 |
| Affected delivery notes | 10 |
| Shipped pieces in containment | 180,242 |

Open [the HTML evidence report](docs/results/index.html), [machine-readable results](docs/results/summary.json), or [the ER diagram](docs/architecture.md). CSV exports are alongside the report.

The complaint lot **L260430-01721** travelled in two barrels while analysis 44 showed **0.926 g/L nickel**, below the configured **1.1 g/L** lower limit. The next analysis showed **0.840 g/L**. The second analysis remains in force through the weekend until Monday's sample; the stored evidence is a validity interval, not a claim that chemistry was continuously measured.

| Customer | Directly plated lots | Rework descendants | Shipped lots | Shipped pieces | Delivery notes |
| --- | ---: | ---: | ---: | ---: | --- |
| C001 | 17 | 8 | 9 | 30,797 | LS-000216, LS-000221 |
| C002 | 14 | 4 | 10 | 26,120 | LS-000217, LS-000222 |
| C003 | 14 | 6 | 7 | 40,702 | LS-000218, LS-000223 |
| C004 | 18 | 5 | 13 | 28,176 | LS-000219, LS-000224 |
| C005 | 17 | 10 | 7 | 54,447 | LS-000220, LS-000225 |

Q1 identifies the suspect bath intervals. Q2 gives quality the complaint's bath evidence: every result that is not plainly OK, including missing or stale evidence (next section). Q3 gives the delivery list for containment, with the shipped quantity and the lot quantity in separate columns. Q4 shows the rework genealogy.

The customer summary lists shipments under the customer named on the delivery note and keeps the ordering customer as a separate column in the detail view; the database also rejects a delivery note addressed to a customer other than the one who ordered the lot. A lot counts as directly plated if it sat in the bath during a bad period itself, and as a descendant if it is only affected through its parent. Affected pieces are counted once per genealogy, so a rework child that holds the same pieces as its affected parent is not counted twice.

## Missing bath evidence

Absence of an analysis is not evidence of a good bath. The backward trace returns one row per plating dwell, analysis in force and parameter, with an explicit status:

| Status | Meaning |
| --- | --- |
| OK, LOW, HIGH | Result compared with the limit version valid on the sampling day |
| NO_LIMIT | A result exists but no limit applies to it (in the simulation: COD, which is monitored without an operating window) |
| MISSING | A parameter that has a limit for the process on the sampling day has no result |
| STALE | The analysis was older than the allowed age while the carrier was in the bath |
| NO_ANALYSIS | Part of the dwell is not covered by any analysis of that tank |

The allowed age comes from `lab.analysis_max_age` per process; without a row, `lab.max_analysis_age()` returns **4 days**. The simulated line samples once per working day at 06:00, so Friday's analysis stays in force until Monday 06:00 (3 days); 4 days also covers a bank holiday Monday. Quality can store a different age per process, and that change is audited.

Lots plated during a NO_ANALYSIS, STALE or MISSING period, together with their descendants, appear in `trace.v_unknown_exposure` ([unknown_exposure.csv](docs/results/unknown_exposure.csv)). This is a separate review list and does not change the containment figures. It is empty for the simulated run, because every dwell is covered by a complete daily analysis.

A result is judged against the limit version that was valid on the **sampling day** (Europe/Berlin), not against every limit version that overlapped the dwell. If a new limit version starts between the sample and the dwell, the trace still shows the status under the limit that applied on the sampling day.

## Run on Windows

On my machine I use a portable PostgreSQL server in `.local/`, listening only on `127.0.0.1:55433`, with credentials in the ignored `.env`. The portable server and the virtual environment are local conveniences, not repository content. From the project folder:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\scripts\local-db.ps1 status
# If stopped: .\scripts\local-db.ps1 start
.\scripts\project.ps1 -Action migrate -Python .\.venv\Scripts\python.exe
.\scripts\project.ps1 -Action demo -Python .\.venv\Scripts\python.exe
```

The demo reuses existing data, runs tests and exports results. It never clears the database. The simulator itself refuses a nonempty project database. Use a new database to change simulation seeds or duration. An existing database built before V009 is brought up to date by the `migrate` action; the new audit triggers only record changes made after that.

Other actions: `migrate`, `test`, `export`, `simulate`, `benchmark`, `backup`. Stop the optional server with `scripts/local-db.ps1 stop` when finished. It is not installed as a Windows service and does not automatically start after reboot.

## Fresh machine with Docker

Install Docker Desktop or Docker Engine. Copy `.env.example` to `.env` and set a local password. Keep `DATABASE_URL` consistent if using host Python; Docker services use the separate `POSTGRES_*` settings.

```powershell
Copy-Item .env.example .env
# Edit .env, then:
docker compose up --build --abort-on-container-exit --exit-code-from demo demo
```

Compose waits for PostgreSQL, applies Flyway migrations, runs the simulator once, executes tests and writes the report to `docs/results/`. I ran this on 2026-10-12 with Docker Desktop on Windows: Flyway 11 migrated a fresh PostgreSQL 17.11 container, the simulator produced the same counts (5,687 lots, 11,662 carriers, 27,733 measurements) and all 44 tests passed. The Docker database starts empty and does not load the Bosch data, so the files committed in `docs/results/` come from my native database.

For an existing PostgreSQL 16/17 server, create an empty database, set `DATABASE_URL`, install `requirements.txt`, then run:

```text
python scripts/migrate.py
python scripts/demo.py
```

The local migration runner checks SHA-256 checksums, holds an advisory lock and applies changes transactionally. It refuses databases managed by Flyway, and it refuses an edited, missing or out-of-order versioned file. Choose one migration runner per database. Versioned migrations are append-only; this project uses V001–V013 plus repeatable role grants (`R__grants.sql`), which re-run after every new versioned migration and repeat the evidence-hardening REVOKEs after their broad GRANTs.

## Real, simulated and assumed

| Data | Origin |
| --- | --- |
| Line throughput, catalogue masses, bath windows and observed production ranges | Published values transcribed from sources cited in `config/sources.yaml`; not measured here |
| Lots, customers, carrier movements, analyses, inspections, complaints | Seeded simulation; no private Partec production records |
| Barrel load, sampling plan, scheduling and planted incident | Explicit assumptions in configuration |
| Bosch production parts and station times | Official Kaggle training data (`train_numeric.csv`, `train_date.csv`), used under the competition rules and not redistributed here |
| Bosch loader test rows | Small invented fixtures, rolled back after each test |

I collected the source catalogue while planning the project and have not independently revalidated every supplier number. Customer codes C001–C005 are fictional; automotive norm names do not establish Partec's customer relationships.

All configured assumptions are exported without omission to [assumptions.json](docs/results/assumptions.json). Core values are 40 kg per barrel, a 1.3 thread-area factor, 40 incoming lots/day, 1–4 carriers/lot, 5 thickness samples/lot, and a nickel test every third lot. Open-ended thickness specifications target minimum + 3 µm. Published production ranges are interpreted as approximately ±2 standard deviations for the simulated spread; that interpretation is an assumption.

The nine-tank layout is illustrative. Only the plating dwell is simulated. Rework children preserve genealogy but await processing; degrease, rinse, passivation and complete rework routes are stretch work. Incident deposit nickel uses an explicit illustrative rule tied to the first load's start time, not a validated electrochemical model. The chemical operating window and deposit-nickel reference come from different published sources and are identified as such.

## Bosch real-data track

I downloaded the archive from the [official Kaggle competition](https://www.kaggle.com/c/bosch-production-line-performance/data) with my own account after accepting the competition rules. The data stays under those rules and is not committed (`data/` is ignored by Git). Extract `train_numeric.csv` and `train_date.csv` into `data/raw/bosch/`, then run:

```text
python ingest/p1_load_bosch.py --max-rows 100000
python ingest/p1_load_bosch.py
python scripts/benchmark_bosch.py
python scripts/check_bosch_time.py
python scripts/export_results.py
```

On 2026-10-12 I ran these steps against my local database (PostgreSQL 17.11 on Windows). The full import loaded **1,183,747 parts** with **6,879 failures (0.581%)** and **14,382,158 station passes** from 52 stations (1,156 date columns) in about six and a half minutes. That is 0.32% above the published part count and 0.001 percentage points from the published failure rate, so no warning was raised ([bosch_import.json](docs/results/bosch_import.json)). The ten most frequent station paths are in [bosch_paths.csv](docs/results/bosch_paths.csv); the most common, L1_S24 > L2_S26 > L3_S29 > L3_S30 > L3_S33 > L3_S34 > L3_S36 > L3_S37, covers 58,379 parts with a 0.67% failure rate.

The loader uses chunked date-file reads and PostgreSQL COPY. It checks unique IDs, binary outcomes, numeric/date ID agreement and finite timestamps. A sample import followed by a full import is safe; records for selected IDs are replaced within one transaction, and failures roll back. A full import records its deviation from the published totals (approximately 1.18 million training parts and 0.58% failures, stored in `config/sources.yaml`) and prints a warning if parts differ by more than 1% or the failure rate by more than 0.02 percentage points; it does not fail the import.

`bosch.v_top_paths` returns the ten most frequent station paths and their failure rates. Equal timestamps are ordered deterministically by station; this cannot recover an unknown physical order.

The time-unit check treats **16.75 ticks/week** (about **2.393 ticks/day**) as a hypothesis from the [published Bosch analysis](https://arxiv.org/abs/1701.00705), reports observed autocorrelations around that interval, and never invents calendar dates. On the full data, the autocorrelation of part arrivals (0.25-tick bins, lags 14–20 ticks) peaks at exactly **16.75 ticks** (r = 0.27) and falls to 0.07–0.14 at the ends of the window ([bosch_time_check.json](docs/results/bosch_time_check.json)). This supports a repeating cycle of that length, but it does not prove that the cycle is a week or give a calendar origin, and the search window was chosen around the hypothesis.

The benchmark saves three `EXPLAIN (ANALYZE, BUFFERS)` runs before and after a temporary `(station_code, t_first)` index for Q8 and Q9. Both queries have a complete `ORDER BY` (Q9 takes the first 100 pairs by part ID), so before and after time the same work; the script hashes each phase's result and stops if they differ. The index is rolled back after the experiment. It does not force index use or assume both queries improve. Medians of three runs on the full data ([bosch_benchmark.json](docs/results/bosch_benchmark.json)): Q9, the search for parts that passed station L3_S32 within 0.05 ticks of a failed part, dropped from **491 ms to 6.6 ms** because the planner switched to the new index; Q8, the failure rate per station, stayed at about **2.6 s** (2,604 ms before, 2,568 ms after) because it reads every station pass and still uses a sequential scan. Both phases returned identical results. These are single-machine timings and will differ on other hardware.

## Verification and operational evidence

The 44 automated checks cover traceability, containment deduplication, delivery recipients (including deferred checks), piece counting by genealogy, geometry, source keys, balanced allocations, barrel capacity, physical-carrier overlap, analysis coverage, missing/stale/uncovered bath evidence, backfilled and duplicate analyses, limit closing, genealogy rules, role denials and attempted evidence rewrites, audited corrections, and Bosch parsing, idempotency and the published-total check.

`scripts/verify_second_seed.py` builds a throwaway database with another seed and start date and checks the planted incident relative to its own placement, not against the demo's fixed counts. With seed 7 from 2026-01-05 both planted analyses were flagged LOW, the complaint lot traced back to one of them, and all 80 lots plated under them were contained; random process spread also produced two further out-of-limit analyses, which containment reports as well ([second_seed.json](docs/results/second_seed.json)).

Roles and audit:

- The lab role can insert analyses, which are audited, but cannot change or delete them and cannot release a lot. The validity ranges are maintained only by the database's own triggers.
- Limit versions can be closed, never edited or deleted; a changed limit is a new version. Quality can start or close a version only from today (Europe/Berlin) onwards, so a limit cannot be withdrawn retroactively; back-dated limit history is reserved for the ingest role and the database owner.
- The read-only role cannot modify measurements, and no application role can attach the database's `SECURITY DEFINER` trigger functions to tables of its own.
- Quality may correct measurement values; the audit demo changes one value and restores it, preserving both audit entries.
- Analyses and their results, limits, the allowed analysis age, carrier runs, carrier allocations, dwell steps, deliveries, lots, parts, measurements and complaints are audited. Each audit row stores `login_user`, the PostgreSQL login that ran the statement, and `changed_by`, the name the application sets in `app.user`. `changed_by` is a label the client chooses, not an authenticated identity. The demo scripts connect with the administrator login and switch to a group role, so `login_user` shows the administrator login. Audit rows written before the hardening migration V009 have no recorded login (`login_user` is NULL); every row written since must have one.
- These roles demonstrate separation of duties in a local demo database. They are group roles without logins; a real deployment needs individual login roles, and the database owner can still change anything, including the audit log.

Tests roll back their changes.

A custom-format `pg_dump` was restored onto a freshly initialised PostgreSQL 17 server: the five group roles were created first (the dump's GRANTs refer to them) and `pg_restore --no-owner` ran as that server's administrator. All 25 tables matched by row count and content hash and the complaint trace returned its two LOW rows; the restore took **1.434 seconds** for a **1,710,627-byte** backup. The source already contained the two demonstration audit entries. See [backup_restore.json](docs/results/backup_restore.json) for the exact snapshot and target. To restore onto another server yourself, either run `pg_dumpall --globals-only` on the source and apply it to the new server first, or create the five `role_*` group roles there; then use `pg_restore --no-owner` if the source's owner login does not exist on the new server. `python scripts/backup_verify.py --target-url postgresql://user@host:port/postgres` automates this drill; without `--target-url` it restores into a new database on the same server. Backups and local database files are ignored by Git.

The workflow in `.github/workflows/ci.yml` is ready for this folder to become its own repository; it also runs the second-seed check. It has not been run on GitHub; local passing tests are not presented as hosted CI results. Before publishing, run hosted CI.

## Known limitations

- Quantity conservation across carrier allocations and deliveries, part mixing within one carrier, and reuse of a physical carrier number in overlapping runs are not enforced by the database; the simulator respects them, and tests check allocations, part mixing and carrier overlap in the simulated data. Child lots must keep their parent's part and customer, may not hold more pieces than the parent, and may not form cycles; carrier steps must lie within their run.
- A delivery note must go to the customer who ordered the lot. Drop shipments to a third party would need that rule relaxed; the containment views already report the recipient.
- Missing-evidence statuses use a fixed default age (4 days) unless quality stores another one per process; the right value depends on the real sampling plan.
- Limits are applied as valid on the sampling day (see above).
- Roles and audit are a local separation-of-duties demonstration, not a hardened security boundary against the database owner.

