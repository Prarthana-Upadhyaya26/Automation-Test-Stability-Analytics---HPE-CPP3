# Defect-to-Test-Run Mapping System

This module provides an automated, zero-shot mapping engine that links JIRA defects to automation test failures. It utilizes state-of-the-art Natural Language Processing (NLP) to perform semantic matching without requiring any model training or labeled datasets.

## How It Works

1. **Data Ingestion**: The system reads test failure records from the main `analytics.db` and defect data from a local `defects.json` file.
2. **Pre-filtering**: Candidates are initially filtered based on temporal proximity (date window) and reporter-to-team identity mapping to ensure relevance.
3. **Semantic Encoding**: Using the `all-MiniLM-L6-v2` sentence-transformer model, defect descriptions and failure messages are converted into 384-dimensional dense vectors.
4. **Scoring**: The system computes a weighted match score (0.0 to 1.0) by combining:
   - **Embedding Similarity**: Cosine similarity between the semantic vectors.
   - **Test Case Match**: Presence of the specific `TC_*` name in the defect text.
   - **Keyword & Category Match**: Overlap in extracted failure keywords and categorizations (e.g., timeout, assertion, data).
   - **Temporal Proximity**: How close the defect creation date is to the test run date.
5. **Output**: The results are beautifully formatted and printed directly to the terminal, highlighting mappings that exceed the confidence threshold.

## Usage

You can run the mapping system directly from the project root. By default, it will automatically locate the required databases and use standard threshold parameters.

```bash
python defect_mapping/main.py
```

### Configuration Flags

For fine-grained control, you can override the default parameters using command-line arguments:

```bash
python defect_mapping/main.py --threshold 0.45 --window 10 --db ./analytics.db
```

| Flag | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `--db` | `string` | `../analytics.db` | Absolute or relative path to the SQLite analytics database. |
| `--defects` | `string` | `./defects.json` | Path to the JSON file containing exported JIRA defects. |
| `--threshold` | `float` | `0.40` | Minimum confidence score (0.0 to 1.0) required to consider a mapping valid. |
| `--window` | `int` | `7` | Maximum number of days between the defect creation and the test run failure. |
| `--model` | `string` | `all-MiniLM-L6-v2` | Hugging Face sentence-transformer model name to use for embeddings. |

> **Note**: For deeper customizations (such as adjusting the exact scoring weights or updating the tester identity map), you can modify the configuration block at the top of `defect_mapping/main.py`.
