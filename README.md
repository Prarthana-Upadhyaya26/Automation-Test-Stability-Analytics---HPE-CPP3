# Synthetic Test Log Generator

Generates realistic Robot Framework XML test reports across multiple CI runs. Useful for testing dashboards, analytics pipelines, or any tooling that consumes `.xml` test result data.

## Files

- `config.py` — all inputs: test definitions, failure behaviour, schedule
- `generate.py` — core logic that builds XML reports and metadata JSON

## How it works

**`config.py`** has three sections:

`DEFAULT_CONFIG` sets run-level parameters like team name, number of runs, output directory, start date, interval between runs, anomaly run numbers (where pass rate drops sharply), and the RNG seed.

`TESTS` is a list of 20 test cases. Each test has an id, name, feature/priority tags, a category (`stable`, `flaky-mild`, `flaky-moderate`, `flaky-heavy`, `consistently_failing`), a base failure probability, a duration pattern, and primary/secondary failure types with a probability split.

`DEPENDENCIES` maps tests to upstream tests they depend on. If a dependency fails in a given run, the downstream test's failure probability is raised using:

```
effective_fail_prob = 1 - (1 - base_fail_prob) * (1 - weight * failed_dep_count)
```

Capped at 0.95.

---

**`generate.py`** is structured as follows:

`gen_*_msg` functions — one per failure type (`timeout`, `element`, `assertion`, `data`, `environment`). Each returns a randomised but realistic error string written into the XML.

`run_pass_rate(n)` — returns the target pass rate for run `n`. Encodes a trend: moderate early → dip mid-run → anomaly spikes → recovery toward the end.

`base_duration / test_duration` — computes per-test execution time. Three tests have special duration shapes (seasonal alternation, a step-change at run 50, progressive drift). Failed tests get extra time added.

`decide_outcome(category, fail_prob, is_anomaly, rng)` — rolls pass/fail for a single test. Anomaly runs push failure probability higher across all categories.

`build_test_xml(...)` — builds the `<test>` XML element with tags, keyword blocks, timestamps, and either a pass status or a structured failure with inner keyword detail.

`build_run(n, config, rng)` — builds a full run:
1. Rolls natural outcomes for all tests (with dependency model applied)
2. Corrects the result set so the final pass rate matches the target — forces some tests to flip; flaky tests are preferred when forcing a pass
3. Calls `build_test_xml` for each test, writes `output.xml` and `ci_metadata.json`

`generate(config)` — outer loop over `num_runs`, calls `build_run`, writes files.

## Output structure

```
./runs/
├── TeamAlpha_build_001/
│   ├── output.xml          # Robot Framework-compatible XML report
│   └── ci_metadata.json    # Pass/fail counts, pass rate, executor, timestamp
├── TeamAlpha_build_002/
│   └── ...
```

## Usage

```bash
# Run with defaults (100 runs → ./runs/)
python generate.py

# Custom options
python generate.py --output-dir ./data --num-runs 50 --start-date 2025-01-01 --interval 12 --seed 7 --team TeamBeta
```

| Argument | Default | Description |
|---|---|---|
| `--output-dir` | `./runs` | Where to write output folders |
| `--num-runs` | `100` | Number of CI runs to generate |
| `--start-date` | `2024-10-01` | Timestamp of run 1 |
| `--interval` | `24` | Hours between runs |
| `--seed` | `42` | RNG seed |
| `--team` | `TeamAlpha` | Team name used in folder names and XML |

To change test definitions, anomaly behaviour, or dependencies, edit `config.py` — no changes to `generate.py` needed.
