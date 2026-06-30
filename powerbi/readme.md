deployed at: https://app.powerbi.com/groups/me/reports/2579f753-3bc7-4fe1-ba73-2c61977327a7/5e8dd5405cd96b0be702?experience=power-bi

# 📊 CI/CD Test Analytics Dashboard (Power BI + SQLite)

This project builds an interactive **Power BI dashboard** on top of a **SQLite database** to analyze CI/CD test execution data.

It provides insights into:

* ✅ System reliability (pass/fail trends)
* ⚡ Performance (execution time)
* 🔍 Root causes of failures
* 🔬 Test stability (flakiness detection)

---

# 🧠 Dashboard Overview

The dashboard is structured into **4 analytical layers**:

## 🟩 1. System Health (KPIs)

* Run Pass Rate
* Failure Rate
* Avg Run Duration
* Flaky Tests

👉 Gives a quick snapshot of overall system health.

---

## 📈 2. Trends

* Pass rate over time
* Execution duration over time

👉 Helps detect regressions and performance degradation.

---

## 📊 3. Failure Analysis

* Pass vs Fail per run
* Top failing tests
* Failure reasons

👉 Identifies what is breaking and why.

---

## 🔬 4. Stability & Performance

* Duration distribution
* Flakiness detection
* Executor performance

👉 Evaluates consistency and infrastructure reliability.

---

# 🧰 Tech Stack

* **Power BI Desktop** – Visualization
* **SQLite** – Database
* **SQLite ODBC Driver** – Connectivity

---

# ⚙️ Setup Instructions

---

## 🔧 Step 1: Install SQLite

Download SQLite tools:

👉 [https://www.sqlite.org/download.html](https://www.sqlite.org/download.html)

### For Windows:

Download:

* `sqlite-tools-win32-x86.zip` (or latest)

Extract the zip file and optionally add it to your system PATH.

---

## 🔧 Step 2: Install SQLite ODBC Driver

Download from:

👉 [http://www.ch-werner.de/sqliteodbc/](http://www.ch-werner.de/sqliteodbc/)

Install:

* `sqliteodbc_w64.exe` (for 64-bit systems)

This allows Power BI to connect to `.db` files.

---

## 🔧 Step 3: Verify ODBC Driver

1. Open:

   ```
   ODBC Data Sources (64-bit)
   ```
2. Go to **Drivers tab**
3. Confirm:

   ```
   SQLite3 ODBC Driver
   ```

---

## 🔧 Step 4: Connect Power BI to SQLite

1. Open **Power BI Desktop**
2. Click:

   ```
   Home → Get Data → ODBC
   ```
3. Select:

   ```
   SQLite3 ODBC Driver
   ```
4. Choose your database file:

   ```
   analytics.db
   ```

---

## 🔧 Step 5: Load Tables

Import:

* `runs`
* `test_results`

---

## 🔗 Step 6: Create Relationships

Go to **Model View** and create:

```
runs[run_id] → test_results[run_id]
```

Cardinality:

```
One-to-Many (runs → test_results)
```

---

## 📐 Step 7: Create Measures (DAX)

### Run Pass Rate

```DAX
Run Pass Rate =
DIVIDE(SUM(runs[passed]), SUM(runs[total]))
```

### Failure Rate

```DAX
Failure Rate =
DIVIDE(SUM(runs[failed]), SUM(runs[total]))
```

### Avg Run Duration

```DAX
Avg Run Duration =
AVERAGE(runs[duration_s])
```

### Flaky Tests

```DAX
Flaky Tests =
CALCULATE(
    DISTINCTCOUNT(test_results[test_name]),
    FILTER(
        VALUES(test_results[test_name]),
        CALCULATE(DISTINCTCOUNT(test_results[status])) > 1
    )
)
```

---

## 📊 Step 8: Build Visuals

### 🟩 KPIs

* Card visuals for key metrics

### 📈 Trend Chart

* X-axis → timestamp
* Y-axis → pass rate, duration

### 📊 Pass vs Fail

* Stacked column chart

### 🔍 Top Failing Tests

* Horizontal bar chart (Top 10)

### 🔍 Failure Reasons

* Bar chart filtered by `status = FAIL`

### 📊 Duration Distribution

* Column chart using bins

### 🔬 Flakiness Detection

* Bar chart using flakiness score

### ⚙️ Executor Performance

* Bar chart comparing agents

---

## 🎛 Step 9: Add Slicers

Recommended slicers:

* `suite_name`
* `timestamp` (date range)
* `status`
* `team` (optional)
* `executor` (optional)

---

## 🔄 Step 10: Refresh Data

Whenever your database updates:

```
Home → Refresh
```

---

# 🚀 Running the Project

1. Open the `.pbix` file
2. Ensure SQLite database path is correct
3. Click **Refresh**
4. Use slicers to explore data

---

# 📤 Sharing

You can share this dashboard via:

* `.pbix` file (best for feedback)
* PDF export (static view)
* Power BI Service (interactive)

---

# 🎯 Key Insights Enabled

This dashboard answers:

* Is the system reliable?
* Which tests fail most frequently?
* What are the main failure causes?
* Which executor is slow or unstable?
* Are tests flaky or consistent?

---

# 📌 Notes

* Use **64-bit ODBC driver** for compatibility
* Ensure `.db` file path is valid
* All measures dynamically respond to slicers

---

# 🧠 What This Project Demonstrates

* Data modeling (relational design)
* DAX measure creation
* Root cause analysis
* Performance monitoring
* Dashboard UX design


