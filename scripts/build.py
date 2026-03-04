#!/usr/bin/env python3
"""
cadence build script
Reads cadence.yaml + items.csv and produces frontend/cadence.json.
Run before deploying: python scripts/build.py
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import yaml


def is_skip_row(row: dict, title_col: str, hours_col: str | None, period_col: str) -> bool:
    """Return True if this row should be skipped (header, total, blank)."""
    title = row.get(title_col, "").strip()
    period = row.get(period_col, "").strip()
    hours = row.get(hours_col, "").strip() if hours_col else ""

    # Blank title
    if not title:
        return True

    # Total rows (e.g. "-- Total hours")
    if title.startswith("--"):
        return True

    # Period is not a valid integer
    try:
        int(period)
    except (ValueError, TypeError):
        return True

    # Has title and period but no hours — likely a section header row
    if hours_col and not hours:
        return True

    return False


def parse_url(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    return v if v.startswith("http") else None


def parse_hours(value: str) -> float | None:
    try:
        return float(value.strip())
    except (ValueError, TypeError, AttributeError):
        return None


def build(config_path: str = "cadence.yaml", output_path: str = "frontend/cadence.json") -> None:
    config_file = Path(config_path)
    if not config_file.exists():
        print(f"Error: config file not found: {config_path}", file=sys.stderr)
        print("Copy cadence.example.yaml to cadence.yaml and fill in your details.")
        sys.exit(1)

    with open(config_file) as f:
        config = yaml.safe_load(f)

    # Resolve CSV path relative to config file
    csv_path = config_file.parent / config.get("csv", "items.csv")
    if not csv_path.exists():
        print(f"Error: CSV not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    # Column mappings
    cols = config.get("columns", {})
    title_col = cols.get("title", "Title")
    period_col = cols.get("period", "Week")
    hours_col = cols.get("hours")
    url_col = cols.get("url")

    # Parse CSV
    items: list[dict] = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if is_skip_row(row, title_col, hours_col, period_col):
                continue
            item = {
                "title": row[title_col].strip(),
                "period": int(row[period_col].strip()),
            }
            if hours_col and hours_col in row:
                item["hours"] = parse_hours(row[hours_col])
            if url_col and url_col in row:
                url = parse_url(row.get(url_col))
                if url:
                    item["url"] = url
            items.append(item)

    if not items:
        print("Warning: no items parsed from CSV — check column names in cadence.yaml")

    # Group by period
    periods: dict[int, list[dict]] = {}
    for item in items:
        p = item["period"]
        periods.setdefault(p, []).append(item)

    # Build period labels + descriptions
    configured_labels = config.get("period_labels", {}) or {}
    configured_descs = config.get("period_descriptions", {}) or {}
    interval = config.get("interval", "week").capitalize()
    period_list = []
    for period_num in sorted(periods.keys()):
        label = configured_labels.get(period_num) or configured_labels.get(str(period_num))
        if not label:
            label = f"{interval} {period_num}"
        desc = configured_descs.get(period_num) or configured_descs.get(str(period_num))
        entry = {
            "number": period_num,
            "label": label,
            "items": periods[period_num],
            "total_hours": round(
                sum(i.get("hours") or 0 for i in periods[period_num]), 2
            ),
        }
        if desc:
            entry["description"] = desc
        period_list.append(entry)

    # Build users list
    users = config.get("users", [])
    if not users:
        print("Warning: no users defined in cadence.yaml")

    # Build output
    output = {
        "name": config.get("name", "Cadence"),
        "description": config.get("description", ""),
        "completion_date": config.get("completion_date"),
        "interval": config.get("interval", "week"),
        "theme": config.get("theme", "default"),
        "users": users,
        "periods": period_list,
        "total_items": len(items),
        "total_hours": round(sum(i.get("hours") or 0 for i in items), 2),
        "aws": config.get("aws", {}),
    }

    # Write output
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"✅ Built cadence.json")
    print(f"   {len(items)} items across {len(periods)} {config.get('interval', 'week')}s")
    print(f"   {len(users)} user(s): {', '.join(u['name'] for u in users)}")
    print(f"   Total hours: {output['total_hours']}")
    print(f"   Output: {out_path}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Build cadence.json from config + CSV")
    parser.add_argument("--config", "-c", default="cadence.yaml")
    parser.add_argument("--output", "-o", default="frontend/cadence.json")
    args = parser.parse_args()

    build(args.config, args.output)
