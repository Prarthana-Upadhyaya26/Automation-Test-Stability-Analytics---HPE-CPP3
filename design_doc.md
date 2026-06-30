# Phase 1 Design Document

## Design Question 1 — Class Balance for the Flakiness Classifier

| Test Name | Category | Fail Probability |
|-----------|----------|-----------------|
| TC_Login_ValidCredentials | stable | 0% |
| TC_Login_InvalidPassword | stable | 0% |
| TC_Login_SessionTimeout | stable | 0% |
| TC_Login_AccountLockout | stable | 0% |
| TC_Dashboard_FilterByDate | stable | 0% |
| TC_Dashboard_Pagination | stable | 0% |
| TC_Dashboard_ExportChart | stable | 0% |
| TC_Dashboard_SearchBar | stable | 0% |
| TC_User_CreateAccount | stable | 0% |
| TC_User_EditProfile | stable | 0% |
| TC_User_DeleteAccount | stable | 0% |
| TC_User_PasswordReset | stable | 0% |
| TC_Login_SSORedirect | flaky-mild | 35% |
| TC_Login_MFAVerification | flaky-mild | 30% |
| TC_Dashboard_LoadWidget | flaky-moderate | 50% |
| TC_Dashboard_RefreshData | flaky-moderate | 55% |
| TC_User_BulkImport | flaky-heavy | 65% |
| TC_User_RoleAssignment | consistently failing | 80% |
| TC_User_BatchExport | consistently failing | 75% |
| TC_Login_OAuthCallback | consistently failing | 70% |

**Category summary:** 12 stable · 2 flaky-mild · 2 flaky-moderate · 1 flaky-heavy · 3 consistently failing

**Rationale:** Five distinct fail probabilities (0.30, 0.35, 0.50, 0.55, 0.65) give the flakiness classifier a meaningful spectrum to learn. The three consistently-failing tests (0.70–0.80) are distinguishable from heavy flaky (0.65) by their run-phase pass-rate curve applied on top.

---

## Design Question 2 — Category Balance for Failure Clustering

Expected failures per test over 100 runs, and failure type distribution:

| Test Name | Primary Failure Type | Secondary Failure Type | Est. failures in 100 runs |
|-----------|---------------------|----------------------|--------------------------|
| TC_Login_SSORedirect | timeout (70%) | element (30%) | ~35 |
| TC_Login_MFAVerification | timeout (70%) | assertion (30%) | ~30 |
| TC_Dashboard_LoadWidget | element (80%) | timeout (20%) | ~50 |
| TC_Dashboard_RefreshData | assertion (60%) | data (40%) | ~55 |
| TC_User_BulkImport | data (70%) | assertion (30%) | ~65 |
| TC_User_RoleAssignment | assertion (65%) | data (35%) | ~80 |
| TC_User_BatchExport | data (65%) | element (35%) | ~75 |
| TC_Login_OAuthCallback | timeout (70%) | element (30%) | ~70 |

**Estimated category totals across ~460 total failures:**

| Category | Estimated count | % |
|----------|-----------------|---|
| timeout  | ~35×0.7 + ~30×0.7 + ~50×0.2 + ~70×0.7 = ~115 | ~25% |
| element  | ~35×0.3 + ~50×0.8 + ~75×0.35 + ~70×0.3 = ~107 | ~23% |
| assertion| ~30×0.3 + ~55×0.6 + ~65×0.3 + ~80×0.65 = ~110 | ~24% |
| data     | ~55×0.4 + ~65×0.7 + ~75×0.65 + ~80×0.35 = ~145 | ~31% |

No single category exceeds 40%. Distribution is balanced enough for clustering to work. The `data` category is slightly higher due to the high-volume consistently-failing tests; acceptable within spec.

---

## Design Question 3 — Duration Patterns for Drift Detection

| Test Name | Duration Pattern | Normal range (s) | Degraded range (s) |
|-----------|-----------------|-----------------|-------------------|
| TC_Login_ValidCredentials | seasonal | 2.0–3.5 (even runs) | 4.5–6.5 (odd runs) |
| TC_Dashboard_ExportChart | step change at run 50 | 3.0–5.0 | 12.0–15.0 |
| TC_User_BulkImport | progressive drift | 10.0–14.0 (runs 1–40) | 28.0–36.0 (runs 66–100) |
| all other 17 tests | normal | 1.2–8.5 | +5–15 on failure |

**Why three patterns matter for ML Phase 4:**
- **Progressive drift** (BulkImport): Requires a rolling Z-score against a baseline window. A static threshold will not detect it because the change is gradual.
- **Step change** (ExportChart): A rolling Z-score with a short window detects the cliff at run 51 as an outlier. Simple and effective.
- **Seasonal** (ValidCredentials): A rolling Z-score will falsely flag odd runs as anomalies. The correct approach is autocorrelation (period=2) or a model that conditions on run parity. This is the key ML insight: the same algorithm that works for step changes fails on seasonal patterns.
