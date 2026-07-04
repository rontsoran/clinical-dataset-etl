"""
Clinical Dataset Import & Analytics — a Tkinter GUI over a dynamic, schema-free
CSV/Excel-to-MySQL ETL pipeline.

The user manually picks any CSV/Excel file via a file dialog. The app then:

1. Loads it with pandas.
2. Filters out non-clinical columns using deterministic rules — no AI/LLM
   classification. Columns are dropped based on name keywords (identifiers,
   free text, URLs/paths, contact info, financial/administrative fields),
   missing-value ratio, and uniqueness ratio (near-unique text like names,
   doctors, hospitals).
3. Infers a MySQL type per remaining column and creates a table named after
   the file (existing tables are only overwritten with explicit confirmation).
4. Batch-inserts the cleaned data, with retry-on-failure.
5. Tracks the import in a DatasetRegistry table.
6. Generates summary statistics, a missing-value report, a correlation
   matrix, and matplotlib visualizations (bar charts, histograms, boxplots,
   correlation heatmap) — shown in the GUI and saved as PNGs.

Nothing runs and no output is produced until the user clicks "Import
Clinical Dataset" — there is no fixed input file and no automatic scanning
at startup.

The import runs on a background thread so the GUI never freezes. Worker
threads communicate with the GUI thread exclusively through thread-safe
Queues, polled periodically via root.after() — they never touch GUI widgets
directly.
"""

import os
import re
import time
import queue
import threading
from datetime import datetime

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog

import pandas as pd
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

import mysql.connector
from mysql.connector import Error as MySQLError

try:
    from config import DB_CONFIG
except ImportError:
    raise SystemExit(
        "Missing config.py. Create it in the project root with a DB_CONFIG dict "
        "(host, user, password) before running the pipeline. See README.md."
    )

# ---------------------------------------------------------------------------
# GLOBAL CONFIG (STRICT MODE: every file this program writes goes here)
# ---------------------------------------------------------------------------
OUTPUT_PATH = os.path.dirname(os.path.abspath(__file__))
DATABASE_NAME = "AgentDB"
MAX_RETRIES = 2

os.makedirs(OUTPUT_PATH, exist_ok=True)

# ---------------------------------------------------------------------------
# CLINICAL DATASET IMPORT (generic CSV/Excel -> filtered -> MySQL -> analytics)
# ---------------------------------------------------------------------------
REGISTRY_TABLE = "DatasetRegistry"
DATASET_BATCH_SIZE = 1000
MAX_CATEGORICAL_CARDINALITY = 20  # skip bar charts for near-unique columns (e.g. names, IDs)

VISUALIZATIONS_DIR = os.path.join(OUTPUT_PATH, "visualizations")
os.makedirs(VISUALIZATIONS_DIR, exist_ok=True)

# Deterministic, rule-based non-clinical column detection — no AI/LLM involved.
# A column is dropped if its (normalized) name contains any keyword from one of
# these groups, regardless of its data.
NON_CLINICAL_KEYWORD_GROUPS = {
    "identifier": ["id", "uuid", "guid", "row number", "rownum", "index"],
    "free-text": ["note", "notes", "comment", "comments", "remark", "remarks",
                  "description", "summary", "narrative"],
    "url/file/image": ["url", "link", "http", "image", "img", "photo", "picture",
                        "path", "filename", "file name", "attachment"],
    "contact info": ["email", "phone", "telephone", "tel", "fax", "address",
                      "zip", "zipcode", "zip code", "postal", "postal code"],
    "financial/administrative": ["billing", "insurance", "payment", "invoice", "cost",
                                  "price", "charge", "room", "ward", "bed number", "bed"],
}
# A column is also dropped if it's mostly/entirely missing, or if it's free-form text
# with too many distinct values to be a clinical category (names, doctors, hospitals, ...).
HIGH_MISSING_RATIO = 0.95
NEAR_UNIQUE_MIN_DISTINCT = 50
NEAR_UNIQUE_RATIO = 0.5

# ---------------------------------------------------------------------------
# THREAD-SAFE COMMUNICATION CHANNELS (worker threads -> GUI thread)
# ---------------------------------------------------------------------------
LOG_QUEUE = queue.Queue()
PROGRESS_QUEUE = queue.Queue()
RESULT_QUEUE = queue.Queue()


def log(message: str):
    """Log to console and push to the GUI's log queue."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line)
    LOG_QUEUE.put(line)


# ---------------------------------------------------------------------------
# DATABASE SETUP
# ---------------------------------------------------------------------------
def ensure_database():
    """Create the database if it doesn't already exist."""
    conn = mysql.connector.connect(**DB_CONFIG)
    cursor = conn.cursor()
    cursor.execute(f"CREATE DATABASE IF NOT EXISTS {DATABASE_NAME}")
    conn.commit()
    cursor.close()
    conn.close()


def get_connection():
    return mysql.connector.connect(database=DATABASE_NAME, **DB_CONFIG)


def _insert_batch_with_retry(cursor, conn, insert_sql, batch):
    """Insert one batch inside a transaction, retrying on failure. No partial commits."""
    attempt = 0
    while attempt <= MAX_RETRIES:
        try:
            cursor.executemany(insert_sql, batch)
            conn.commit()
            log(f"[IMPORT] Inserted batch of {len(batch)} records.")
            return len(batch)
        except MySQLError as e:
            conn.rollback()
            attempt += 1
            log(f"[IMPORT] Batch insert failed (attempt {attempt}/{MAX_RETRIES}): {e}")
            time.sleep(0.2 * attempt)

    log(f"[IMPORT] Dropping batch of {len(batch)} records after {MAX_RETRIES} retries.")
    return 0


# ---------------------------------------------------------------------------
# CLINICAL DATASET IMPORT: deterministic column filtering + generic MySQL loading
# ---------------------------------------------------------------------------
def sanitize_identifier(name: str) -> str:
    """Turn an arbitrary file/column name into a safe MySQL identifier."""
    cleaned = re.sub(r"[^0-9a-zA-Z_]", "_", str(name).strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_") or "col"
    if cleaned[0].isdigit():
        cleaned = f"_{cleaned}"
    return cleaned


def ensure_registry_table():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {REGISTRY_TABLE} (
            DatasetID INT AUTO_INCREMENT PRIMARY KEY,
            DatasetName VARCHAR(150) UNIQUE,
            SourceFile VARCHAR(255),
            RowCount INT,
            ColumnCount INT,
            LoadedAt DATETIME
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def get_registered_dataset_names() -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(f"SELECT DatasetName FROM {REGISTRY_TABLE} ORDER BY DatasetName")
    names = [row[0] for row in cursor.fetchall()]
    cursor.close()
    conn.close()
    return names


def register_dataset(dataset_name: str, source_file: str, row_count: int, column_count: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(f"""
        INSERT INTO {REGISTRY_TABLE} (DatasetName, SourceFile, RowCount, ColumnCount, LoadedAt)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            SourceFile = VALUES(SourceFile),
            RowCount = VALUES(RowCount),
            ColumnCount = VALUES(ColumnCount),
            LoadedAt = VALUES(LoadedAt)
    """, (dataset_name, source_file, row_count, column_count, datetime.now()))
    conn.commit()
    cursor.close()
    conn.close()


def dataset_table_exists(table_name: str) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SHOW TABLES LIKE %s", (table_name,))
    exists = cursor.fetchone() is not None
    cursor.close()
    conn.close()
    return exists


def _normalize_for_matching(name) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(name).lower()).strip()


def _matches_keyword_group(normalized_name: str, keywords) -> bool:
    padded = f" {normalized_name} "
    return any(f" {kw} " in padded for kw in keywords)


def classify_column(col_name, series) -> tuple:
    """Deterministic, rule-based clinical-relevance check for one column.

    Returns (keep: bool, reason: str). No AI/LLM classification — just column
    name keyword matching, dtype, missing-value ratio, and uniqueness ratio.
    """
    normalized = _normalize_for_matching(col_name)
    for group_name, keywords in NON_CLINICAL_KEYWORD_GROUPS.items():
        if _matches_keyword_group(normalized, keywords):
            return False, f"{group_name} field"

    non_null = series.notna().sum()
    if non_null == 0:
        return False, "empty column"

    missing_ratio = 1 - (non_null / len(series))
    if missing_ratio >= HIGH_MISSING_RATIO:
        return False, "almost entirely missing"

    if pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series):
        distinct = series.nunique(dropna=True)
        uniqueness_ratio = distinct / non_null
        if distinct > NEAR_UNIQUE_MIN_DISTINCT and uniqueness_ratio > NEAR_UNIQUE_RATIO:
            return False, "near-unique text (likely identifier/name/contact field)"

    return True, "kept"


def filter_clinical_columns(df: pd.DataFrame):
    """Drop non-clinical columns. Returns (filtered_df, dropped[(name, reason), ...])."""
    kept_columns = []
    dropped = []
    for col in df.columns:
        keep, reason = classify_column(col, df[col])
        if keep:
            kept_columns.append(col)
        else:
            dropped.append((col, reason))
    return df[kept_columns].copy(), dropped


def build_missing_value_report(df: pd.DataFrame) -> list:
    lines = ["\nMissing Value Report:"]
    total_rows = len(df)
    if total_rows == 0:
        lines.append("  (no rows)")
        return lines
    for col in df.columns:
        missing = int(df[col].isna().sum())
        pct = missing / total_rows * 100
        lines.append(f"  {col:<25}: {missing} missing ({pct:.1f}%)")
    return lines


def _clean_and_infer(df: pd.DataFrame):
    """Fill missing values and infer a MySQL column type for every column, without a fixed schema."""
    column_types = {}
    for col in df.columns:
        series = df[col]
        if pd.api.types.is_bool_dtype(series):
            df[col] = series.fillna(False)
            column_types[col] = "BOOLEAN"
        elif pd.api.types.is_integer_dtype(series):
            df[col] = series.fillna(0).astype("int64")
            column_types[col] = "BIGINT"
        elif pd.api.types.is_float_dtype(series):
            df[col] = series.fillna(0.0)
            column_types[col] = "DOUBLE"
        else:
            parsed_dates = pd.to_datetime(series, errors="coerce")
            non_null = series.notna().sum()
            if non_null > 0 and parsed_dates.notna().sum() / non_null > 0.9:
                df[col] = parsed_dates
                column_types[col] = "DATETIME"
            else:
                df[col] = series.fillna("Unknown").astype(str)
                max_len = df[col].str.len().max() if len(df[col]) else 0
                column_types[col] = "TEXT" if max_len and max_len > 255 else "VARCHAR(255)"
    return df, column_types


def _to_db_value(value):
    if pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if hasattr(value, "item"):
        return value.item()
    return value


def create_dataset_table(table_name: str, column_types: dict):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(f"DROP TABLE IF EXISTS `{table_name}`")
    columns_sql = ",\n            ".join(f"`{col}` {sql_type}" for col, sql_type in column_types.items())
    cursor.execute(f"""
        CREATE TABLE `{table_name}` (
            _row_id INT AUTO_INCREMENT PRIMARY KEY,
            {columns_sql}
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def import_clinical_dataset(path: str, overwrite_confirmed: bool = False):
    """Manually-triggered dataset import: load, filter to clinically
    relevant columns, infer schema, store in MySQL, then analyze and visualize.
    """
    try:
        ensure_database()

        filename = os.path.basename(path)
        dataset_name = sanitize_identifier(os.path.splitext(filename)[0])
        ext = os.path.splitext(filename)[1].lower()

        log(f"[IMPORT] Loading {filename}...")
        df_raw = pd.read_csv(path) if ext == ".csv" else pd.read_excel(path)
        row_count = len(df_raw)
        columns_before = len(df_raw.columns)

        df_raw = df_raw.rename(columns={c: sanitize_identifier(c) for c in df_raw.columns})
        df_filtered, dropped_columns = filter_clinical_columns(df_raw)
        columns_after = len(df_filtered.columns)

        log(f"[IMPORT] {filename}: {row_count} rows, {columns_before} columns -> "
            f"{columns_after} clinically relevant columns.")
        for col, reason in dropped_columns:
            log(f"[IMPORT] Dropped column '{col}' ({reason}).")

        RESULT_QUEUE.put({
            "import_preview": True,
            "filename": filename,
            "rows": row_count,
            "columns_before": columns_before,
            "columns_after": columns_after,
        })

        if columns_after == 0:
            raise ValueError("No clinically relevant columns remained after filtering — cannot import this file.")

        ensure_registry_table()
        if dataset_table_exists(dataset_name) and not overwrite_confirmed:
            RESULT_QUEUE.put({"import_needs_confirmation": True, "dataset_name": dataset_name, "path": path})
            return

        missing_lines = build_missing_value_report(df_filtered)
        df_clean, column_types = _clean_and_infer(df_filtered)
        columns = list(df_clean.columns)

        create_dataset_table(dataset_name, column_types)

        insert_sql = (
            f"INSERT INTO `{dataset_name}` (" + ", ".join(f"`{c}`" for c in columns) + ") "
            f"VALUES (" + ", ".join(["%s"] * len(columns)) + ")"
        )

        RESULT_QUEUE.put({"total_records": len(df_clean)})

        conn = get_connection()
        cursor = conn.cursor()
        total_inserted = 0
        batch = []
        for row in df_clean.itertuples(index=False, name=None):
            batch.append(tuple(_to_db_value(v) for v in row))
            if len(batch) >= DATASET_BATCH_SIZE:
                total_inserted += _insert_batch_with_retry(cursor, conn, insert_sql, batch)
                batch = []
                PROGRESS_QUEUE.put(total_inserted)
        if batch:
            total_inserted += _insert_batch_with_retry(cursor, conn, insert_sql, batch)
            PROGRESS_QUEUE.put(total_inserted)
        cursor.close()
        conn.close()

        register_dataset(dataset_name, filename, len(df_clean), len(columns))
        log(f"[IMPORT] Loaded {total_inserted} rows into table '{dataset_name}'.")

        analyze_and_visualize_dataset(dataset_name, extra_report_lines=missing_lines)
        RESULT_QUEUE.put({"datasets": get_registered_dataset_names()})
    except Exception as e:
        log(f"[IMPORT] ERROR: {e}")
        RESULT_QUEUE.put({"fatal_error": str(e)})


def analyze_dataset(dataset_name: str, extra_report_lines: list = None) -> dict:
    """Generic analytics for any loaded dataset table: distributions, stats, correlations."""
    conn = get_connection()
    df = pd.read_sql(f"SELECT * FROM `{dataset_name}`", conn)
    conn.close()
    df = df.drop(columns=["_row_id"], errors="ignore")

    numeric_cols = df.select_dtypes(include="number").columns.tolist()
    categorical_cols = [c for c in df.columns if c not in numeric_cols]

    lines = [f"Dataset Analytics Report: {dataset_name}", "=" * 60,
             f"Rows: {len(df)}   Columns: {len(df.columns)}"]

    if extra_report_lines:
        lines.extend(extra_report_lines)

    if numeric_cols:
        lines.append("\nNumeric Column Statistics:")
        stats = df[numeric_cols].describe().T
        for col, row in stats.iterrows():
            lines.append(
                f"  {col:<25}: count={row['count']:.0f} mean={row['mean']:.2f} "
                f"min={row['min']:.2f} max={row['max']:.2f}"
            )

    if categorical_cols:
        lines.append("\nCategorical Column Distributions (top 5 per column):")
        for col in categorical_cols:
            lines.append(f"  {col}:")
            for value, count in df[col].value_counts().head(5).items():
                lines.append(f"    {value}: {count}")

    if len(numeric_cols) >= 2:
        lines.append("\nCorrelation Matrix (numeric columns):")
        lines.append(df[numeric_cols].corr().round(2).to_string())

    report = "\n".join(lines)
    report_path = os.path.join(OUTPUT_PATH, f"{dataset_name}_analytics_report.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    log(f"[DATASET ANALYTICS] Report saved to {report_path}")

    return {
        "df": df,
        "numeric_cols": numeric_cols,
        "categorical_cols": categorical_cols,
        "report_path": report_path,
    }


def generate_visualizations(dataset_name: str, df: pd.DataFrame, numeric_cols: list, categorical_cols: list) -> list:
    """Build bar/histogram/boxplot/heatmap figures, save each as PNG, and return them for display."""
    dataset_viz_dir = os.path.join(VISUALIZATIONS_DIR, dataset_name)
    os.makedirs(dataset_viz_dir, exist_ok=True)
    figures = []

    for col in categorical_cols:
        if df[col].nunique(dropna=True) > MAX_CATEGORICAL_CARDINALITY:
            continue
        counts = df[col].value_counts().head(MAX_CATEGORICAL_CARDINALITY)
        if counts.empty:
            continue
        fig = Figure(figsize=(7, 4.5), dpi=100)
        ax = fig.add_subplot(111)
        ax.bar(counts.index.astype(str), counts.values, color="#00d8a0")
        ax.set_title(f"{col} — Distribution")
        ax.set_ylabel("Count")
        ax.tick_params(axis="x", rotation=45)
        fig.tight_layout()
        fig.savefig(os.path.join(dataset_viz_dir, f"bar_{col}.png"))
        figures.append((f"{col} — Bar Chart", fig))

    for col in numeric_cols:
        values = df[col].dropna()
        if values.empty:
            continue

        fig = Figure(figsize=(7, 4.5), dpi=100)
        ax = fig.add_subplot(111)
        ax.hist(values, bins=20, color="#00d8a0")
        ax.set_title(f"{col} — Histogram")
        fig.tight_layout()
        fig.savefig(os.path.join(dataset_viz_dir, f"hist_{col}.png"))
        figures.append((f"{col} — Histogram", fig))

        fig2 = Figure(figsize=(4, 4.5), dpi=100)
        ax2 = fig2.add_subplot(111)
        ax2.boxplot(values)
        ax2.set_title(f"{col} — Boxplot")
        fig2.tight_layout()
        fig2.savefig(os.path.join(dataset_viz_dir, f"box_{col}.png"))
        figures.append((f"{col} — Boxplot", fig2))

    if len(numeric_cols) >= 2:
        corr = df[numeric_cols].corr()
        fig3 = Figure(figsize=(6, 5), dpi=100)
        ax3 = fig3.add_subplot(111)
        im = ax3.imshow(corr.values, cmap="viridis")
        ax3.set_xticks(range(len(numeric_cols)))
        ax3.set_xticklabels(numeric_cols, rotation=45, ha="right")
        ax3.set_yticks(range(len(numeric_cols)))
        ax3.set_yticklabels(numeric_cols)
        fig3.colorbar(im, ax=ax3)
        ax3.set_title("Correlation Heatmap")
        fig3.tight_layout()
        fig3.savefig(os.path.join(dataset_viz_dir, "correlation_heatmap.png"))
        figures.append(("Correlation Heatmap", fig3))

    return figures


def analyze_and_visualize_dataset(dataset_name: str, extra_report_lines: list = None):
    try:
        log(f"[DATASET ANALYTICS] Analyzing '{dataset_name}'...")
        result = analyze_dataset(dataset_name, extra_report_lines=extra_report_lines)
        figures = generate_visualizations(
            dataset_name, result["df"], result["numeric_cols"], result["categorical_cols"]
        )
        log(f"[DATASET ANALYTICS] Generated {len(figures)} visualizations for '{dataset_name}' "
            f"(saved to {os.path.join(VISUALIZATIONS_DIR, dataset_name)}).")
        RESULT_QUEUE.put({
            "dataset_analysis_done": True,
            "dataset_name": dataset_name,
            "report_path": result["report_path"],
            "figures": figures,
        })
    except Exception as e:
        log(f"[DATASET ANALYTICS] ERROR analyzing '{dataset_name}': {e}")
        RESULT_QUEUE.put({"fatal_error": str(e)})


def export_dataset_to_excel(dataset_name: str) -> str:
    """Export an imported dataset's MySQL table to an Excel file."""
    conn = get_connection()
    df = pd.read_sql(f"SELECT * FROM `{dataset_name}`", conn)
    conn.close()
    df = df.drop(columns=["_row_id"], errors="ignore")
    export_path = os.path.join(OUTPUT_PATH, f"{dataset_name}_export.xlsx")
    df.to_excel(export_path, index=False)
    return export_path


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
BG = "#12151c"
PANEL_BG = "#1a1e29"
ACCENT = "#00d8a0"
ACCENT_DIM = "#0a8f6c"
TEXT_MAIN = "#e6e9ef"
TEXT_DIM = "#8b93a6"
DANGER = "#ff5d6c"
WARN = "#ffb454"
LOG_BG = "#0a0d13"
LOG_FG = "#7ce8c4"


class DatasetImportGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("📊 Clinical Dataset Import & Analytics")
        self.geometry("980x700")
        self.minsize(860, 600)
        self.configure(bg=BG)

        self._dataset_busy = False
        self._open_figure_windows = []
        self._build_style()
        self._build_layout()
        self._poll_queues()

    # -- styling ------------------------------------------------------
    def _build_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL_BG)
        style.configure("Header.TLabel", background=BG, foreground=TEXT_MAIN,
                         font=("Segoe UI", 18, "bold"))
        style.configure("SubHeader.TLabel", background=BG, foreground=TEXT_DIM,
                         font=("Segoe UI", 10))
        style.configure("Status.TLabel", background=BG, foreground=ACCENT,
                         font=("Segoe UI", 10, "bold"))
        style.configure("Info.TLabel", background=PANEL_BG, foreground=TEXT_MAIN,
                         font=("Segoe UI", 9))

        style.configure("Accent.TButton", background=ACCENT, foreground="#04140f",
                         font=("Segoe UI", 11, "bold"), padding=10, borderwidth=0)
        style.map("Accent.TButton",
                  background=[("active", ACCENT_DIM), ("disabled", "#2a3040")],
                  foreground=[("disabled", "#5b6272")])

        style.configure("Ghost.TButton", background=PANEL_BG, foreground=TEXT_MAIN,
                         font=("Segoe UI", 10), padding=8, borderwidth=0)
        style.map("Ghost.TButton",
                  background=[("active", "#242a38"), ("disabled", PANEL_BG)],
                  foreground=[("disabled", "#4c5261")])

        style.configure("Horizontal.TProgressbar", troughcolor=PANEL_BG,
                         background=ACCENT, bordercolor=PANEL_BG,
                         lightcolor=ACCENT, darkcolor=ACCENT, thickness=14)

        style.configure("TCombobox", fieldbackground=PANEL_BG, background=PANEL_BG,
                         foreground=TEXT_MAIN, arrowcolor=TEXT_MAIN)
        style.map("TCombobox", fieldbackground=[("readonly", PANEL_BG)],
                  foreground=[("readonly", TEXT_MAIN)])

    # -- layout ---------------------------------------------------------
    def _build_layout(self):
        root = ttk.Frame(self, padding=18)
        root.pack(fill="both", expand=True)

        header = ttk.Frame(root)
        header.pack(fill="x")
        ttk.Label(header, text="📊 Clinical Dataset Import & Analytics", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="Upload any CSV/Excel file — non-clinical columns are filtered out automatically, "
                 "then the dataset is stored in MySQL and analyzed.",
            style="SubHeader.TLabel",
        ).pack(anchor="w", pady=(2, 12))

        # Dataset import panel (primary and only focus of the dashboard)
        dataset_panel = ttk.Frame(root, style="Panel.TFrame", padding=14)
        dataset_panel.pack(fill="x", pady=(0, 14))

        controls_row = ttk.Frame(dataset_panel, style="Panel.TFrame")
        controls_row.pack(fill="x")

        self.import_button = ttk.Button(
            controls_row, text="📥 Import Clinical Dataset", style="Accent.TButton",
            command=self.browse_and_import_dataset,
        )
        self.import_button.pack(side="left")

        self.dataset_combo = ttk.Combobox(controls_row, state="readonly", width=26)
        self.dataset_combo.pack(side="left", padx=(10, 0))

        self.view_charts_button = ttk.Button(
            controls_row, text="📈 View Charts", style="Ghost.TButton",
            command=self.view_selected_dataset_charts,
        )
        self.view_charts_button.pack(side="left", padx=(10, 0))
        ttk.Button(controls_row, text="🗒 View Report", style="Ghost.TButton",
                   command=self.view_selected_dataset_report).pack(side="left", padx=(10, 0))
        ttk.Button(controls_row, text="📤 Export to Excel", style="Ghost.TButton",
                   command=self.export_selected_dataset_to_excel).pack(side="left", padx=(10, 0))

        self.status_label = ttk.Label(controls_row, text="● Idle", style="Status.TLabel", background=PANEL_BG)
        self.status_label.pack(side="right")

        info_row = ttk.Frame(dataset_panel, style="Panel.TFrame")
        info_row.pack(fill="x", pady=(10, 0))

        self.dataset_rows_label = ttk.Label(info_row, text="Rows: —", style="Info.TLabel")
        self.dataset_rows_label.pack(side="left")
        self.dataset_cols_label = ttk.Label(info_row, text="Columns: — → —", style="Info.TLabel")
        self.dataset_cols_label.pack(side="left", padx=(16, 0))

        self.dataset_status_label = ttk.Label(info_row, text="No dataset imported yet",
                                               style="SubHeader.TLabel", background=PANEL_BG)
        self.dataset_status_label.pack(side="right")

        self.progress = ttk.Progressbar(
            dataset_panel, style="Horizontal.TProgressbar", maximum=1, value=0
        )
        self.progress.pack(fill="x", pady=(10, 0))

        # Log console
        log_header = ttk.Frame(root)
        log_header.pack(fill="x")
        ttk.Label(log_header, text="Activity Log", style="SubHeader.TLabel").pack(side="left")
        ttk.Button(log_header, text="🗑 Clear Log", style="Ghost.TButton",
                   command=self._clear_log).pack(side="right")

        log_frame = ttk.Frame(root, style="Panel.TFrame", padding=1)
        log_frame.pack(fill="both", expand=True, pady=(6, 0))
        self.log_text = scrolledtext.ScrolledText(
            log_frame, bg=LOG_BG, fg=LOG_FG, insertbackground=LOG_FG,
            font=("Consolas", 10), relief="flat", wrap="word", state="disabled",
        )
        self.log_text.pack(fill="both", expand=True, padx=1, pady=1)

    # -- dataset import actions ----------------------------------------------
    def browse_and_import_dataset(self):
        if self._dataset_busy:
            return
        path = filedialog.askopenfilename(
            title="Select a clinical dataset (CSV or Excel)",
            filetypes=[("CSV or Excel files", "*.csv *.xlsx *.xls"), ("All files", "*.*")],
        )
        if not path:
            return

        self._dataset_busy = True
        self.import_button.configure(state="disabled")
        self.view_charts_button.configure(state="disabled")
        self.status_label.configure(text="● Importing...", foreground=WARN)
        self.progress.configure(value=0)
        self.dataset_rows_label.configure(text="Rows: —")
        self.dataset_cols_label.configure(text="Columns: — → —")
        self.dataset_status_label.configure(text=f"Importing {os.path.basename(path)}...")
        threading.Thread(target=import_clinical_dataset, args=(path,), daemon=True).start()

    def view_selected_dataset_charts(self):
        if self._dataset_busy:
            return
        dataset_name = self.dataset_combo.get()
        if not dataset_name:
            messagebox.showinfo("No dataset selected", "Import a dataset and pick one from the dropdown first.")
            return

        self._dataset_busy = True
        self.import_button.configure(state="disabled")
        self.view_charts_button.configure(state="disabled")
        self.status_label.configure(text="● Analyzing...", foreground=WARN)
        self.dataset_status_label.configure(text=f"Analyzing '{dataset_name}'...")
        threading.Thread(target=analyze_and_visualize_dataset, args=(dataset_name,), daemon=True).start()

    def view_selected_dataset_report(self):
        dataset_name = self.dataset_combo.get()
        if not dataset_name:
            messagebox.showinfo("No dataset selected", "Import a dataset and pick one from the dropdown first.")
            return
        report_path = os.path.join(OUTPUT_PATH, f"{dataset_name}_analytics_report.txt")
        self._open_report(report_path, f"{dataset_name} — Analytics Report")

    def export_selected_dataset_to_excel(self):
        dataset_name = self.dataset_combo.get()
        if not dataset_name:
            messagebox.showinfo("No dataset selected", "Import a dataset and pick one from the dropdown first.")
            return
        threading.Thread(target=self._export_dataset_worker, args=(dataset_name,), daemon=True).start()

    def _export_dataset_worker(self, dataset_name):
        try:
            path = export_dataset_to_excel(dataset_name)
            log(f"[EXPORT] '{dataset_name}' exported to {path}")
            RESULT_QUEUE.put({"dataset_export_done": True, "path": path, "dataset_name": dataset_name})
        except Exception as e:
            log(f"[EXPORT] ERROR exporting '{dataset_name}': {e}")
            RESULT_QUEUE.put({"fatal_error": str(e)})

    def _show_figures(self, dataset_name, figures):
        for title, fig in figures:
            window = tk.Toplevel(self)
            window.title(f"{dataset_name} — {title}")
            window.configure(bg=BG)
            canvas = FigureCanvasTkAgg(fig, master=window)
            canvas.draw()
            canvas.get_tk_widget().pack(fill="both", expand=True)
            self._open_figure_windows.append(window)

    def _open_report(self, path, title):
        if not os.path.exists(path):
            messagebox.showinfo("Not available yet", f"{title} hasn't been generated yet.")
            return

        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        window = tk.Toplevel(self)
        window.title(title)
        window.geometry("760x560")
        window.configure(bg=BG)

        ttk.Label(window, text=title, style="Header.TLabel", background=BG).pack(
            anchor="w", padx=14, pady=(14, 6)
        )
        text_widget = scrolledtext.ScrolledText(
            window, bg=LOG_BG, fg=TEXT_MAIN, font=("Consolas", 10), relief="flat", wrap="word"
        )
        text_widget.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        text_widget.insert("1.0", content)
        text_widget.configure(state="disabled")

    def _open_file_externally(self, path, label):
        if not os.path.exists(path):
            messagebox.showinfo("Not available yet", f"{label} hasn't been generated yet.")
            return
        os.startfile(path)

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # -- queue polling (runs on the GUI/main thread) -----------------------
    def _poll_queues(self):
        while True:
            try:
                line = LOG_QUEUE.get_nowait()
            except queue.Empty:
                break
            self.log_text.configure(state="normal")
            self.log_text.insert("end", line + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

        while True:
            try:
                inserted = PROGRESS_QUEUE.get_nowait()
            except queue.Empty:
                break
            self.progress.configure(value=inserted)

        while True:
            try:
                result = RESULT_QUEUE.get_nowait()
            except queue.Empty:
                break
            self._handle_result(result)

        self.after(120, self._poll_queues)

    def _handle_result(self, result: dict):
        if "fatal_error" in result:
            self._dataset_busy = False
            self.import_button.configure(state="normal")
            self.view_charts_button.configure(state="normal")
            self.status_label.configure(text="● Error", foreground=DANGER)
            messagebox.showerror("Error", result["fatal_error"])
            return

        if "total_records" in result:
            self.progress.configure(maximum=result["total_records"], value=0)

        if result.get("import_preview"):
            self.dataset_rows_label.configure(text=f"Rows: {result['rows']}")
            self.dataset_cols_label.configure(
                text=f"Columns: {result['columns_before']} → {result['columns_after']}"
            )

        if result.get("import_needs_confirmation"):
            self._dataset_busy = False
            self.import_button.configure(state="normal")
            self.view_charts_button.configure(state="normal")
            self.status_label.configure(text="● Idle", foreground=ACCENT)
            dataset_name = result["dataset_name"]
            path = result["path"]
            if messagebox.askyesno(
                "Table already exists",
                f"A dataset table named '{dataset_name}' already exists.\n\n"
                "Overwrite it with the newly selected file?",
            ):
                self._dataset_busy = True
                self.import_button.configure(state="disabled")
                self.view_charts_button.configure(state="disabled")
                self.status_label.configure(text="● Importing...", foreground=WARN)
                threading.Thread(target=import_clinical_dataset, args=(path, True), daemon=True).start()
            else:
                self.dataset_status_label.configure(text="Import cancelled.")

        if "datasets" in result:
            self._dataset_busy = False
            self.import_button.configure(state="normal")
            self.view_charts_button.configure(state="normal")
            self.status_label.configure(text="● Idle", foreground=ACCENT)
            datasets = result["datasets"]
            self.dataset_combo.configure(values=datasets)
            if datasets and not self.dataset_combo.get():
                self.dataset_combo.set(datasets[0])
            self.dataset_status_label.configure(
                text=f"{len(datasets)} dataset(s) imported" if datasets else "No dataset imported yet"
            )

        if result.get("dataset_analysis_done"):
            self._dataset_busy = False
            self.import_button.configure(state="normal")
            self.view_charts_button.configure(state="normal")
            self.status_label.configure(text="● Idle", foreground=ACCENT)
            dataset_name = result["dataset_name"]
            self.dataset_status_label.configure(text=f"'{dataset_name}' analyzed — {len(result['figures'])} charts")
            self._show_figures(dataset_name, result["figures"])

        if result.get("dataset_export_done"):
            self.dataset_status_label.configure(text=f"Exported '{result['dataset_name']}' to Excel.")
            self._open_file_externally(result["path"], result["dataset_name"])


if __name__ == "__main__":
    app = DatasetImportGUI()
    app.mainloop()
