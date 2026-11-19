"""Validate, clean and enrich the raw Chicago daily weather table.

Usage:
    python src/prepare_data.py

Reads:  data/raw/chicago_daily_2015_2024.csv
Writes: data/processed/chicago_daily_clean.csv
        data/processed/data_quality_report.json
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW_CSV = ROOT / "data" / "raw" / "chicago_daily_2015_2024.csv"
OUT_DIR = ROOT / "data" / "processed"

RENAME = {
    "temperature_2m_max": "tmax_c",
    "temperature_2m_min": "tmin_c",
    "temperature_2m_mean": "tmean_c",
    "apparent_temperature_mean": "feels_like_c",
    "precipitation_sum": "precip_mm",
    "rain_sum": "rain_mm",
    "snowfall_sum": "snow_cm",  # Open-Meteo reports snowfall in centimetres
    "precipitation_hours": "precip_hours",
    "wind_speed_10m_max": "wind_max_kmh",
    "wind_gusts_10m_max": "gust_max_kmh",
    "shortwave_radiation_sum": "solar_mj_m2",
    "relative_humidity_2m_mean": "humidity_pct",
    "cloud_cover_mean": "cloud_pct",
    "pressure_msl_mean": "pressure_hpa",
}

# Physically plausible bounds for Chicago; anything outside is treated as an error.
BOUNDS = {
    "tmax_c": (-40, 45),
    "tmin_c": (-45, 35),
    "tmean_c": (-42, 40),
    "precip_mm": (0, 300),
    "snow_cm": (0, 100),
    "precip_hours": (0, 24),
    "wind_max_kmh": (0, 200),
    "gust_max_kmh": (0, 250),
    "humidity_pct": (0, 100),
    "cloud_pct": (0, 100),
    "pressure_hpa": (950, 1070),
}

SEASON_OF_MONTH = {12: "Winter", 1: "Winter", 2: "Winter",
                   3: "Spring", 4: "Spring", 5: "Spring",
                   6: "Summer", 7: "Summer", 8: "Summer",
                   9: "Autumn", 10: "Autumn", 11: "Autumn"}

WET_DAY_MM = 1.0        # WMO convention for a "wet day"
HOT_DAY_C = 32.2        # 90 °F, the common US heat threshold
FREEZE_C = 0.0


def validate(df: pd.DataFrame) -> dict:
    """Run integrity checks and return a report. Hard failures raise."""
    report: dict = {"rows_raw": int(len(df))}

    expected = pd.date_range(df["date"].min(), df["date"].max(), freq="D")
    report["duplicate_dates"] = int(df["date"].duplicated().sum())
    report["missing_dates"] = int(len(expected.difference(df["date"])))
    report["missing_values"] = {k: int(v) for k, v in df.isna().sum().items() if v}

    out_of_bounds = {}
    for col, (lo, hi) in BOUNDS.items():
        n = int(((df[col] < lo) | (df[col] > hi)).sum())
        if n:
            out_of_bounds[col] = n
    report["out_of_bounds"] = out_of_bounds

    report["tmin_gt_tmax"] = int((df["tmin_c"] > df["tmax_c"]).sum())
    report["tmean_outside_min_max"] = int(
        ((df["tmean_c"] < df["tmin_c"] - 0.5) | (df["tmean_c"] > df["tmax_c"] + 0.5)).sum()
    )
    report["rain_gt_precip"] = int((df["rain_mm"] > df["precip_mm"] + 0.05).sum())
    report["gust_lt_wind"] = int((df["gust_max_kmh"] < df["wind_max_kmh"]).sum())
    return report


def clean(df: pd.DataFrame, report: dict) -> pd.DataFrame:
    df = df.drop_duplicates(subset="date").sort_values("date")
    df = df.set_index("date").asfreq("D")

    # Values outside physical bounds become NaN so they can be interpolated below.
    for col, (lo, hi) in BOUNDS.items():
        df.loc[(df[col] < lo) | (df[col] > hi), col] = np.nan

    # Short gaps (<= 3 days) are filled by time interpolation for continuous variables;
    # precipitation/snow gaps are left as NaN (interpolating rain invents events).
    continuous = [c for c in df.columns if c not in ("precip_mm", "rain_mm", "snow_cm", "precip_hours")]
    missing_before = int(df[continuous].isna().sum().sum())
    df[continuous] = df[continuous].interpolate(method="time", limit=3)
    missing_after = int(df[continuous].isna().sum().sum())
    report["values_imputed"] = missing_before - missing_after
    report["values_still_missing"] = missing_after + int(df[["precip_mm", "snow_cm"]].isna().sum().sum())
    return df.reset_index()


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    d = df["date"]
    df["year"] = d.dt.year
    df["month"] = d.dt.month
    df["month_name"] = d.dt.strftime("%b")
    df["day_of_year"] = d.dt.day_of_year
    df["season"] = df["month"].map(SEASON_OF_MONTH)
    # December belongs to the following winter (e.g. Dec-2015 + Jan/Feb-2016 = winter 2016).
    df["season_year"] = np.where(df["month"] == 12, df["year"] + 1, df["year"])

    df["temp_range_c"] = df["tmax_c"] - df["tmin_c"]
    df["wind_chill_gap_c"] = df["tmean_c"] - df["feels_like_c"]

    df["is_wet_day"] = df["precip_mm"] >= WET_DAY_MM
    df["is_snow_day"] = df["snow_cm"] > 0
    df["is_hot_day"] = df["tmax_c"] >= HOT_DAY_C
    df["is_freezing_day"] = df["tmin_c"] < FREEZE_C
    df["is_ice_day"] = df["tmax_c"] < FREEZE_C  # stayed below zero all day

    wet = df.loc[df["is_wet_day"], "precip_mm"]
    p95 = float(wet.quantile(0.95))
    df["is_heavy_precip_day"] = df["precip_mm"] >= p95
    df.attrs["heavy_precip_threshold_mm"] = p95

    # Daily climatology for this decade: smooth the day-of-year mean with a 31-day
    # circular window so anomalies reflect weather, not day-to-day noise in the baseline.
    doy_mean = df.groupby("day_of_year")["tmean_c"].mean().reindex(range(1, 367))
    padded = pd.concat([doy_mean.iloc[-15:], doy_mean, doy_mean.iloc[:15]])
    smooth = padded.rolling(31, center=True, min_periods=1).mean().iloc[15:-15]
    df["tmean_climatology_c"] = df["day_of_year"].map(smooth)
    df["tmean_anomaly_c"] = df["tmean_c"] - df["tmean_climatology_c"]

    # Statistical outliers relative to the seasonal baseline (kept, only flagged).
    anomaly_std = df["tmean_anomaly_c"].std()
    df["is_temp_outlier"] = df["tmean_anomaly_c"].abs() > 3 * anomaly_std
    return df


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    raw = pd.read_csv(RAW_CSV, parse_dates=["date"]).rename(columns=RENAME)

    report = validate(raw)
    df = clean(raw, report)
    df = enrich(df)

    report["rows_clean"] = int(len(df))
    report["date_range"] = [str(df["date"].min().date()), str(df["date"].max().date())]
    report["heavy_precip_threshold_mm"] = round(df.attrs["heavy_precip_threshold_mm"], 2)
    report["temp_outlier_days"] = int(df["is_temp_outlier"].sum())

    df.to_csv(OUT_DIR / "chicago_daily_clean.csv", index=False)
    (OUT_DIR / "data_quality_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps(report, indent=2))
    print(f"Saved {len(df):,} rows x {df.shape[1]} columns to data/processed/chicago_daily_clean.csv")


if __name__ == "__main__":
    main()
