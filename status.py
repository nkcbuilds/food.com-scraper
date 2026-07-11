import argparse
import csv
import json
import subprocess
import sys
import time
from collections import OrderedDict
from pathlib import Path


TOTAL_RECIPES = 522_568

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def row_recipe_id(row):
    return (row.get("RecipeId") or row.get("\ufeffRecipeId") or "").strip()


def count_csv_rows(path):
    if not path.exists() or path.stat().st_size == 0:
        return 0, 0

    rows = 0
    unique_ids = set()
    with open(path, "r", newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows += 1
            recipe_id = row.get("RecipeId")
            if recipe_id:
                unique_ids.add(recipe_id)
    return rows, len(unique_ids)


def count_csv_rows_with_valid_ids(path):
    if not path.exists() or path.stat().st_size == 0:
        return 0, 0, 0

    rows = 0
    valid_ids = set()
    bad_rows = 0
    with open(path, "r", newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows += 1
            recipe_id = row_recipe_id(row)
            if recipe_id.isdigit():
                valid_ids.add(recipe_id)
            else:
                bad_rows += 1
    return rows, len(valid_ids), bad_rows


def tail_text(path, lines=8):
    if not path.exists() or path.stat().st_size == 0:
        return []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.readlines()[-lines:]


def latest_files(path, limit=5):
    if not path.exists():
        return []
    files = [p for p in path.iterdir() if p.is_file() and not p.name.startswith(".tmp-")]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:limit]


def running_scrapers():
    command = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { ($_.CommandLine -match 'scraper.py' -or $_.CommandLine -match 'recovery_scraper.py') -and $_.Name -ne 'powershell.exe' } | "
        "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return []

    text = result.stdout.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    return data


def build_status(root):
    root = Path(root)
    images_dir = root / "images"
    recovery_images_dir = root / "recovery_images"
    logs_dir = root / "logs"

    log_paths = OrderedDict(
        [
            ("downloaded", logs_dir / "downloaded.csv"),
            ("missing", logs_dir / "missing.csv"),
            ("failed", logs_dir / "failed.csv"),
            ("skipped_existing", logs_dir / "skipped_existing.csv"),
        ]
    )
    recovery_log_paths = OrderedDict(
        [
            ("recovery_downloaded", logs_dir / "recovery_downloaded.csv"),
            ("recovery_missing", logs_dir / "recovery_missing.csv"),
            ("recovery_failed", logs_dir / "recovery_failed.csv"),
        ]
    )

    counts = {}
    processed_ids = set()
    for name, path in log_paths.items():
        rows, unique, bad_rows = count_csv_rows_with_valid_ids(path)
        counts[name] = {"rows": rows, "unique_recipe_ids": unique, "bad_rows": bad_rows}
        if path.exists() and path.stat().st_size > 0:
            with open(path, "r", newline="", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    recipe_id = row_recipe_id(row)
                    if recipe_id.isdigit():
                        processed_ids.add(recipe_id)

    recovery_counts = {}
    recovery_processed_ids = set()
    recovery_bad_rows = 0
    for name, path in recovery_log_paths.items():
        rows, unique, bad_rows = count_csv_rows_with_valid_ids(path)
        recovery_counts[name] = {"rows": rows, "unique_recipe_ids": unique, "bad_rows": bad_rows}
        recovery_bad_rows += bad_rows
        if path.exists() and path.stat().st_size > 0:
            with open(path, "r", newline="", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    recipe_id = row_recipe_id(row)
                    if recipe_id.isdigit():
                        recovery_processed_ids.add(recipe_id)

    image_files = latest_files(images_dir, limit=5)
    image_count = len([p for p in images_dir.iterdir() if p.is_file() and not p.name.startswith(".tmp-")]) if images_dir.exists() else 0
    temp_files = len(list(images_dir.glob(".tmp-*"))) if images_dir.exists() else 0
    recovery_image_files = latest_files(recovery_images_dir, limit=5)
    recovery_image_count = len([p for p in recovery_images_dir.iterdir() if p.is_file() and not p.name.startswith(".tmp-")]) if recovery_images_dir.exists() else 0
    recovery_temp_files = len(list(recovery_images_dir.glob(".tmp-*"))) if recovery_images_dir.exists() else 0

    processed = len(processed_ids)
    percent = (processed / TOTAL_RECIPES) * 100 if TOTAL_RECIPES else 0
    throttle_state = {}
    throttle_path = logs_dir / "request_throttle.json"
    if throttle_path.exists():
        try:
            with open(throttle_path, "r", encoding="utf-8") as f:
                throttle_state = json.load(f)
        except json.JSONDecodeError:
            throttle_state = {"error": "invalid throttle state json"}
    recovery_summary = {}
    summary_path = logs_dir / "recovery_summary.json"
    if summary_path.exists():
        try:
            with open(summary_path, "r", encoding="utf-8") as f:
                recovery_summary = json.load(f)
        except json.JSONDecodeError:
            recovery_summary = {"error": "invalid recovery summary json"}
    index_path = Path(recovery_summary.get("index_path") or logs_dir / "foodcom_recipe_index.csv")
    index_rows = 0
    if index_path.exists() and index_path.stat().st_size > 0:
        try:
            with open(index_path, "r", newline="", encoding="utf-8-sig", errors="replace") as f:
                index_rows = max(0, sum(1 for _ in f) - 1)
        except OSError:
            index_rows = 0

    return {
        "root": root,
        "images_dir": images_dir,
        "recovery_images_dir": recovery_images_dir,
        "logs_dir": logs_dir,
        "image_count": image_count,
        "recovery_image_count": recovery_image_count,
        "temp_files": temp_files,
        "recovery_temp_files": recovery_temp_files,
        "counts": counts,
        "recovery_counts": recovery_counts,
        "processed_unique_recipe_ids": processed,
        "recovery_processed_unique_recipe_ids": len(recovery_processed_ids),
        "recovery_bad_rows": recovery_bad_rows,
        "total_recipes": TOTAL_RECIPES,
        "percent": percent,
        "latest_images": image_files,
        "latest_recovery_images": recovery_image_files,
        "latest_recovery_downloads": tail_text(logs_dir / "recovery_downloaded.csv", lines=5),
        "latest_recovery_failures": tail_text(logs_dir / "recovery_failed.csv", lines=5),
        "throttle_state": throttle_state,
        "recovery_summary": recovery_summary,
        "index_path": index_path,
        "index_rows": index_rows,
        "scrapers": running_scrapers(),
        "stderr_tail": tail_text(logs_dir / "scrape_stderr.log"),
    }


def print_status(status):
    print("Food.com image scraper status")
    print("=" * 31)
    print(f"Images folder: {status['images_dir']}")
    print(f"Recovery image folder: {status['recovery_images_dir']}")
    print(f"Logs folder:   {status['logs_dir']}")
    print(f"Images saved:  {status['image_count']}")
    print(f"Recovery images saved: {status['recovery_image_count']}")
    print(f"Temp files:    {status['temp_files']}")
    print(f"Recovery temp files: {status['recovery_temp_files']}")
    print(
        "Processed IDs: "
        f"{status['processed_unique_recipe_ids']:,} / {status['total_recipes']:,} "
        f"({status['percent']:.2f}%)"
    )
    print()

    print("Logs")
    for name, data in status["counts"].items():
        suffix = f" bad_rows={data['bad_rows']:,}" if data["bad_rows"] else ""
        print(f"  {name:17} rows={data['rows']:,} valid_ids={data['unique_recipe_ids']:,}{suffix}")
    print()

    print("Recovery logs")
    for name, data in status["recovery_counts"].items():
        suffix = f" bad_rows={data['bad_rows']:,}" if data["bad_rows"] else ""
        print(f"  {name:17} rows={data['rows']:,} valid_ids={data['unique_recipe_ids']:,}{suffix}")
    print(f"  recovery processed unique IDs: {status['recovery_processed_unique_recipe_ids']:,}")
    print(f"  recovery mode: {status['recovery_summary'].get('mode', 'unknown')}")
    print(f"  index: {status['index_path']} ({status['index_rows']:,} rows)")
    print()

    print("Running scraper processes")
    if not status["scrapers"]:
        print("  none found")
    for proc in status["scrapers"]:
        print(f"  PID {proc.get('ProcessId')}: {proc.get('CommandLine')}")
    print()

    print("Latest images")
    if not status["latest_images"]:
        print("  none found")
    for path in status["latest_images"]:
        stat = path.stat()
        print(f"  {path.name:16} {stat.st_size:>9,} bytes")
    print()

    print("Latest recovery images")
    if not status["latest_recovery_images"]:
        print("  none found")
    for path in status["latest_recovery_images"]:
        stat = path.stat()
        print(f"  {path.name:16} {stat.st_size:>9,} bytes")
    print()

    print("Recovery output tail")
    if not status["latest_recovery_downloads"]:
        print("  no recovery downloads logged yet")
    for line in status["latest_recovery_downloads"]:
        print("  " + line.rstrip())
    if status["latest_recovery_failures"]:
        print("  recent recovery failures:")
        for line in status["latest_recovery_failures"]:
            print("  " + line.rstrip())
    print()

    print("Shared throttle")
    throttle = status["throttle_state"]
    if not throttle:
        print("  no throttle state found yet")
    else:
        cooldown_until = float(throttle.get("cooldown_until", 0) or 0)
        now = time.time()
        if cooldown_until > now:
            print(f"  cooldown active for {int(cooldown_until - now)}s: {throttle.get('cooldown_reason', '')}")
        else:
            print(f"  last_request_at={throttle.get('last_request_at', 'unknown')}")
            if throttle.get("cooldown_reason"):
                print(f"  last cooldown reason={throttle.get('cooldown_reason')}")
    print()

    print("Progress output tail")
    if not status["stderr_tail"]:
        print("  no stderr progress output found")
    for line in status["stderr_tail"]:
        print("  " + line.rstrip())


def main():
    parser = argparse.ArgumentParser(description="Show Food.com scraper progress without stopping it.")
    parser.add_argument("--root", default=".", help="Repository root")
    parser.add_argument("--watch", type=int, default=0, help="Refresh every N seconds")
    args = parser.parse_args()

    while True:
        print_status(build_status(args.root))
        if args.watch <= 0:
            return
        time.sleep(args.watch)
        print("\n")


if __name__ == "__main__":
    main()
