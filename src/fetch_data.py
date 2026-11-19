"""Download daily historical weather for Chicago (2015-2024) from the Open-Meteo Archive API.

Usage:
    python src/fetch_data.py

Writes:
    data/raw/chicago_daily_2015_2024.json   (untouched API response)
    data/raw/chicago_daily_2015_2024.csv    (same data as a flat table)
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"

API_URL = "https://archive-api.open-meteo.com/v1/archive"

# Chicago Loop (downtown). Open-Meteo snaps this to the nearest ERA5 grid cell.
LOCATION = {"name": "Chicago, IL", "latitude": 41.8781, "longitude": -87.6298}
START_DATE = "2015-01-01"
END_DATE = "2024-12-31"

DAILY_VARIABLES = [
    "temperature_2m_max",
    "temperature_2m_min",
    "temperature_2m_mean",
    "apparent_temperature_mean",
    "precipitation_sum",
    "rain_sum",
    "snowfall_sum",
    "precipitation_hours",
    "wind_speed_10m_max",
    "wind_gusts_10m_max",
    "shortwave_radiation_sum",
    "relative_humidity_2m_mean",
    "cloud_cover_mean",
    "pressure_msl_mean",
]


def fetch(retries: int = 4, backoff: float = 3.0) -> dict:
    params = {
        "latitude": LOCATION["latitude"],
        "longitude": LOCATION["longitude"],
        "start_date": START_DATE,
        "end_date": END_DATE,
        "daily": ",".join(DAILY_VARIABLES),
        "timezone": "America/Chicago",
        "temperature_unit": "celsius",
        "wind_speed_unit": "kmh",
        "precipitation_unit": "mm",
    }
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(API_URL, params=params, timeout=60)
            if resp.status_code == 400:
                # Bad request is not transient (e.g. unknown variable) - surface the API's reason.
                raise ValueError(f"Open-Meteo rejected the request: {resp.json().get('reason')}")
            resp.raise_for_status()
            return resp.json()
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            last_error = exc
            wait = backoff * attempt
            print(f"Attempt {attempt} failed ({exc}); retrying in {wait:.0f}s")
            time.sleep(wait)
    raise RuntimeError(f"Failed after {retries} attempts") from last_error


def to_frame(payload: dict) -> pd.DataFrame:
    daily = payload["daily"]
    df = pd.DataFrame(daily).rename(columns={"time": "date"})
    df["date"] = pd.to_datetime(df["date"])
    return df


def main() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    payload = fetch()

    json_path = RAW_DIR / "chicago_daily_2015_2024.json"
    json_path.write_text(json.dumps(payload, indent=1), encoding="utf-8")

    df = to_frame(payload)
    csv_path = RAW_DIR / "chicago_daily_2015_2024.csv"
    df.to_csv(csv_path, index=False)

    print(f"Grid cell: lat={payload['latitude']}, lon={payload['longitude']}, "
          f"elevation={payload.get('elevation')} m")
    print(f"Rows: {len(df):,}  |  {df['date'].min().date()} -> {df['date'].max().date()}")
    print(f"Saved {json_path.relative_to(ROOT)} and {csv_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
