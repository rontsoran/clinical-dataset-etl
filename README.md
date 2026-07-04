# 📊 Clinical Dataset Import & Analytics

A desktop Tkinter application that turns any CSV or Excel file into a MySQL table, automatically filtered down to clinically relevant columns, with generated statistics and matplotlib visualizations — no fixed schema, no manual SQL.

You pick a file, the app does the rest: load → filter → infer types → create a table → insert → analyze → visualize. Nothing runs until you click the import button — there is no bundled sample data and no automatic startup behavior.

> This project ships with no sample data. Any file you upload should be fictional or de-identified — no real patient information should be processed by or stored in this project.

![Python](https://img.shields.io/badge/python-3.x-blue)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)
![MySQL](https://img.shields.io/badge/database-MySQL%208.0-orange)

---

## Features

- **Manual, upload-triggered import** — click **📥 Import Clinical Dataset**, pick any `.csv`, `.xlsx`, or `.xls` file via a native file dialog
- **Deterministic column filtering** — drops identifiers, free-text, URLs/paths, contact info, financial/administrative fields, near-empty columns, and near-unique text (names, doctors, hospitals) using keyword and heuristic rules, not AI/LLM classification
- **Automatic type inference** — each surviving column is mapped to `BOOLEAN`, `BIGINT`, `DOUBLE`, `DATETIME`, `VARCHAR(255)`, or `TEXT` and given its own dynamically created MySQL table (named after the file)
- **Safe overwrite handling** — importing a file whose table name already exists prompts for explicit confirmation before dropping and recreating it
- **Batch insert with retry** — rows are inserted in batches of 1,000 inside a transaction, retrying up to twice on failure before dropping a batch
- **Dataset registry** — every import is tracked in a `DatasetRegistry` table so previously loaded datasets remain selectable across runs
- **Auto-generated analytics report** — missing-value report, numeric column statistics, top-5 categorical distributions, and a correlation matrix, saved as `<dataset_name>_analytics_report.txt`
- **Auto-generated visualizations** — bar charts (skipped for high-cardinality columns), histograms, boxplots, and a correlation heatmap, saved as PNGs and shown in the GUI
- **Excel export** — dump any imported dataset's MySQL table back out to `<dataset_name>_export.xlsx`
- **Responsive GUI** — the import/analysis work runs on a background thread and reports progress through thread-safe queues, so the Tkinter UI never freezes

---

## How column filtering works

`filter_clinical_columns()` runs every column through `classify_column()`, which drops a column if **any** of the following are true:

**1. Name matches a non-clinical keyword group** (case-insensitive, whole-word match against the normalized column name):

| Group | Keywords |
|---|---|
| Identifier | `id`, `uuid`, `guid`, `row number`, `rownum`, `index` |
| Free-text | `note`, `notes`, `comment`, `comments`, `remark`, `remarks`, `description`, `summary`, `narrative` |
| URL/file/image | `url`, `link`, `http`, `image`, `img`, `photo`, `picture`, `path`, `filename`, `file name`, `attachment` |
| Contact info | `email`, `phone`, `telephone`, `tel`, `fax`, `address`, `zip`, `zipcode`, `zip code`, `postal`, `postal code` |
| Financial/administrative | `billing`, `insurance`, `payment`, `invoice`, `cost`, `price`, `charge`, `room`, `ward`, `bed number`, `bed` |

**2. Missing-value ratio** — dropped if `>= 95%` of values are missing (`HIGH_MISSING_RATIO`).

**3. Near-unique text** — for text/object columns, dropped if the column has more than `50` distinct values (`NEAR_UNIQUE_MIN_DISTINCT`) **and** distinct values make up more than `50%` of non-null rows (`NEAR_UNIQUE_RATIO`) — catches free-form identifiers like names, doctors, or hospitals that a keyword match alone wouldn't catch.

Every dropped column is logged with its reason. If filtering removes every column, the import is aborted.

---

## Architecture / flow

```
User picks a CSV/Excel file
 (📥 Import Clinical Dataset)
              │
              ▼
   pandas.read_csv / read_excel
              │
              ▼
   filter_clinical_columns()   ──► drop non-clinical columns (keyword / missing / uniqueness rules)
              │
              ▼
   _clean_and_infer()          ──► fill missing values, infer a MySQL type per column
              │
              ▼
   create_dataset_table()      ──► CREATE TABLE `<dataset_name>` (existing table dropped only after
              │                     explicit Yes/No confirmation)
              ▼
   _insert_batch_with_retry()  ──► batched INSERT, retry up to MAX_RETRIES on failure
              │
              ▼
   register_dataset()          ──► upsert into DatasetRegistry
              │
              ▼
   analyze_dataset()           ──► <dataset_name>_analytics_report.txt
              │
              ▼
   generate_visualizations()   ──► PNGs under visualizations/<dataset_name>/, shown in the GUI
```

The worker runs on a background `threading.Thread`. It never touches Tkinter widgets directly — it communicates with the GUI thread exclusively through three `queue.Queue` instances (`LOG_QUEUE`, `PROGRESS_QUEUE`, `RESULT_QUEUE`), polled every 120ms via `root.after()`.

---

## GUI

Running `main.py` opens a single-panel dashboard (`DatasetImportGUI`):

- **📥 Import Clinical Dataset** — opens a file dialog (CSV/Excel) and imports the selected file
- **Dataset dropdown** — lists every dataset imported this session or in the past (backed by `DatasetRegistry`)
- **📈 View Charts** — re-runs analytics + visualization on the selected dataset and opens each chart in its own window
- **🗒 View Report** — opens the selected dataset's analytics report in a scrollable text window
- **📤 Export to Excel** — exports the selected dataset's table to `<dataset_name>_export.xlsx` and opens it
- **🗑 Clear Log** — clears the activity log console
- **Progress bar** — tracks rows inserted during import
- **Row / column preview labels** — show row count and the before → after column count once filtering completes
- **Status label** — Idle / Importing… / Analyzing… / Error
- **Activity log** — live, timestamped log of every step (load, dropped columns, batch inserts, analytics, visualization, export)

---

## DatasetRegistry schema

Created automatically by `ensure_registry_table()`:

| Column | Type | Notes |
|---|---|---|
| `DatasetID` | `INT AUTO_INCREMENT PRIMARY KEY` | |
| `DatasetName` | `VARCHAR(150) UNIQUE` | Sanitized file name, also used as the dataset's table name |
| `SourceFile` | `VARCHAR(255)` | Original uploaded file name |
| `RowCount` | `INT` | Rows loaded |
| `ColumnCount` | `INT` | Columns kept after filtering |
| `LoadedAt` | `DATETIME` | Updated on every (re-)import via `ON DUPLICATE KEY UPDATE` |

Each dataset also gets its own table (named after the sanitized file name) with a `_row_id INT AUTO_INCREMENT PRIMARY KEY` plus one column per surviving field, typed per `_clean_and_infer()`. There is no fixed application schema — `ensure_database()` only creates the `AgentDB` database itself.

---

## Prerequisites

- Python 3.9+
- MySQL Server 8.0+ (running locally or reachable over the network)
- Windows (tested on Windows; Tkinter/matplotlib are cross-platform, but `export_dataset_to_excel`'s "open file" behavior uses `os.startfile`, which is Windows-only)

---

## Setup

1. **Clone the repository**

   ```bash
   git clone <repository-url>
   cd clinical-dataset-etl
   ```

2. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

3. **Create `config.py`** in the project root with your MySQL credentials (this file is git-ignored and never committed):

   ```python
   DB_CONFIG = {
       "host": "localhost",
       "user": "root",
       "password": "your-mysql-password",
   }
   ```

   The database itself (`AgentDB`) is created automatically on first import — no manual SQL setup required.

## Running

```bash
python main.py
```

Click **📥 Import Clinical Dataset** and pick a CSV or Excel file, then watch the activity log.

A ready-to-use sample is included at `examples/Healthcare_dataset.csv` (a public, synthetic healthcare dataset — no real patient data) if you want to try the tool immediately without supplying your own file.

---

## Project structure

```
clinical-dataset-etl/
├── main.py                   # Entire application: ETL, analytics, visualization, GUI
├── config.py                 # MySQL credentials (git-ignored, create it yourself)
├── requirements.txt          # Python dependencies
├── .gitignore
├── README.md
├── examples/
│   └── Healthcare_dataset.csv       # Bundled sample dataset (synthetic, public)
├── <dataset>_analytics_report.txt   # Generated per imported dataset
├── <dataset>_export.xlsx            # Generated on demand via Export to Excel
└── visualizations/
    └── <dataset_name>/
        ├── bar_<column>.png
        ├── hist_<column>.png
        ├── box_<column>.png
        └── correlation_heatmap.png
```

---

## Tech stack

- **Python** — pandas for data loading/cleaning, matplotlib for charts
- **Tkinter / ttk** — GUI, threading + queues for a non-blocking background worker
- **MySQL** (`mysql-connector-python`) — dynamic per-dataset tables plus a `DatasetRegistry`
- **openpyxl** — Excel read/write support for pandas

---

## License

No license specified. All rights reserved by the repository owner unless otherwise stated.
