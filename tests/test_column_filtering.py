"""Unit tests for the deterministic, rule-based clinical column filter and
type-inference logic in main.py. No MySQL connection or GUI is involved —
every function under test is a pure function of its inputs.
"""
import datetime

import numpy as np
import pandas as pd

import main


class TestSanitizeIdentifier:
    def test_spaces_become_underscores(self):
        assert main.sanitize_identifier("Blood Type") == "Blood_Type"

    def test_collapses_repeated_separators(self):
        assert main.sanitize_identifier("Room   Number!!") == "Room_Number"

    def test_leading_digit_gets_prefixed(self):
        assert main.sanitize_identifier("123abc") == "_123abc"

    def test_symbols_only_falls_back_to_col(self):
        assert main.sanitize_identifier("???") == "col"

    def test_strips_leading_trailing_underscores(self):
        assert main.sanitize_identifier("__Age__") == "Age"


class TestClassifyColumn:
    def test_identifier_keyword_dropped(self):
        keep, reason = main.classify_column("Patient ID", pd.Series(range(10)))
        assert keep is False
        assert "identifier" in reason

    def test_free_text_keyword_dropped(self):
        keep, reason = main.classify_column("Notes", pd.Series(["a note"] * 10))
        assert keep is False
        assert "free-text" in reason

    def test_financial_keyword_dropped(self):
        keep, reason = main.classify_column("Billing Amount", pd.Series([100.0] * 10))
        assert keep is False
        assert "financial" in reason

    def test_contact_info_keyword_dropped(self):
        keep, reason = main.classify_column("Email Address", pd.Series(["a@b.com"] * 10))
        assert keep is False
        assert "contact" in reason

    def test_url_keyword_dropped(self):
        keep, reason = main.classify_column("Profile URL", pd.Series(["http://x"] * 10))
        assert keep is False
        assert "url" in reason

    def test_empty_column_dropped(self):
        keep, reason = main.classify_column("Something", pd.Series([None] * 10))
        assert keep is False
        assert "empty" in reason

    def test_almost_entirely_missing_dropped(self):
        series = pd.Series([None] * 99 + [1])
        keep, reason = main.classify_column("Rare Value", series)
        assert keep is False
        assert "missing" in reason

    def test_near_unique_text_dropped(self):
        series = pd.Series([f"Person {i}" for i in range(60)])
        keep, reason = main.classify_column("Full Name", series)
        assert keep is False
        assert "unique" in reason

    def test_low_cardinality_categorical_kept(self):
        keep, _ = main.classify_column("Gender", pd.Series(["Male", "Female"] * 30))
        assert keep is True

    def test_numeric_column_never_dropped_by_uniqueness_rule(self):
        # Every value is distinct, but it's numeric, so the text-uniqueness rule
        # must not apply to it (an Age or ID-like numeric column with all-unique
        # values is still legitimate clinical/numeric data).
        keep, _ = main.classify_column("Age", pd.Series(range(60)))
        assert keep is True

    def test_moderate_cardinality_below_threshold_kept(self):
        # 40 distinct values is below NEAR_UNIQUE_MIN_DISTINCT (50), so it
        # should survive even though every value is unique.
        series = pd.Series([f"Cat{i}" for i in range(40)])
        keep, _ = main.classify_column("Category", series)
        assert keep is True


class TestFilterClinicalColumns:
    def test_drops_non_clinical_keeps_clinical(self):
        df = pd.DataFrame({
            "Patient ID": range(60),
            "Full Name": [f"Person {i}" for i in range(60)],
            "Age": [30 + (i % 40) for i in range(60)],
            "Gender": ["Male", "Female"] * 30,
            "Billing Amount": [100.0] * 60,
            "Diagnosis": ["Flu", "Healthy", "Diabetes"] * 20,
        })
        filtered, dropped = main.filter_clinical_columns(df)
        dropped_names = {name for name, _ in dropped}
        assert dropped_names == {"Patient ID", "Full Name", "Billing Amount"}
        assert list(filtered.columns) == ["Age", "Gender", "Diagnosis"]

    def test_dropped_list_includes_reason(self):
        df = pd.DataFrame({"Room Number": [101, 102, 103]})
        _, dropped = main.filter_clinical_columns(df)
        assert len(dropped) == 1
        assert dropped[0][0] == "Room Number"
        assert "financial" in dropped[0][1]

    def test_all_columns_clinical_drops_nothing(self):
        df = pd.DataFrame({"Age": [30, 40, 50], "Diagnosis": ["Flu", "Healthy", "Flu"]})
        filtered, dropped = main.filter_clinical_columns(df)
        assert dropped == []
        assert list(filtered.columns) == ["Age", "Diagnosis"]


class TestBuildMissingValueReport:
    def test_reports_correct_percentage(self):
        df = pd.DataFrame({"Age": [30, None, 40, None]})
        joined = "\n".join(main.build_missing_value_report(df))
        assert "Age" in joined
        assert "2 missing" in joined
        assert "50.0%" in joined

    def test_empty_dataframe_handled(self):
        lines = main.build_missing_value_report(pd.DataFrame())
        assert any("no rows" in line for line in lines)


class TestCleanAndInfer:
    def test_integer_column_filled_and_typed(self):
        # A plain Python list with None mixed into integers becomes float64 in
        # pandas — use the nullable Int64 extension dtype to genuinely exercise
        # the integer branch (this is what happens for an "Int64"-backed source
        # column, e.g. from certain Excel/CSV readers or explicit dtype casts).
        series = pd.Series(pd.array([30, None, 40], dtype="Int64"))
        cleaned, types_ = main._clean_and_infer(pd.DataFrame({"Age": series}))
        assert types_["Age"] == "BIGINT"
        assert cleaned["Age"].isna().sum() == 0

    def test_plain_int_with_missing_falls_back_to_double(self):
        # Documents real behavior: a plain (non-nullable-dtype) integer column
        # with a missing value becomes float64 before it ever reaches this
        # function, so it's correctly typed DOUBLE, not BIGINT.
        cleaned, types_ = main._clean_and_infer(pd.DataFrame({"Age": [30, None, 40]}))
        assert types_["Age"] == "DOUBLE"
        assert cleaned["Age"].isna().sum() == 0

    def test_float_column_typed_double(self):
        cleaned, types_ = main._clean_and_infer(pd.DataFrame({"BMI": [22.5, None, 30.1]}))
        assert types_["BMI"] == "DOUBLE"
        assert cleaned["BMI"].isna().sum() == 0

    def test_boolean_column_typed_boolean(self):
        series = pd.Series(pd.array([True, False, None], dtype="boolean"))
        cleaned, types_ = main._clean_and_infer(pd.DataFrame({"Flag": series}))
        assert types_["Flag"] == "BOOLEAN"

    def test_date_like_text_inferred_as_datetime(self):
        df = pd.DataFrame({"Admission Date": ["2024-01-01", "2024-02-15", "2024-03-20"]})
        _, types_ = main._clean_and_infer(df)
        assert types_["Admission Date"] == "DATETIME"

    def test_free_text_column_typed_varchar_and_filled(self):
        cleaned, types_ = main._clean_and_infer(pd.DataFrame({"Diagnosis": ["Flu", "Healthy", None]}))
        assert types_["Diagnosis"] == "VARCHAR(255)"
        assert (cleaned["Diagnosis"] == "Unknown").sum() == 1

    def test_long_text_typed_as_text(self):
        df = pd.DataFrame({"Description": ["x" * 300, "short"]})
        _, types_ = main._clean_and_infer(df)
        assert types_["Description"] == "TEXT"


class TestToDbValue:
    def test_nan_becomes_none(self):
        assert main._to_db_value(float("nan")) is None

    def test_timestamp_converted_to_python_datetime(self):
        result = main._to_db_value(pd.Timestamp("2024-01-01"))
        assert isinstance(result, datetime.datetime)

    def test_numpy_scalar_converted_to_native_python_type(self):
        result = main._to_db_value(np.int64(42))
        assert result == 42
        assert isinstance(result, int)

    def test_plain_value_passthrough(self):
        assert main._to_db_value("hello") == "hello"
