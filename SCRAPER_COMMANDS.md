# Scraper Commands

## Where Things Are Stored

Images are stored here:

```powershell
S:\Image scraper\images
```

Recovery scraper images are stored separately here:

```powershell
S:\Image scraper\recovery_images
```

Logs are stored here:

```powershell
S:\Image scraper\logs
```

Main log files:

- `logs\downloaded.csv` - successful image downloads.
- `logs\missing.csv` - recipes where no accurate image was found.
- `logs\failed.csv` - temporary or unexpected failures.
- `logs\skipped_existing.csv` - recipes skipped because a valid image already existed.
- `logs\scrape_stderr.log` - live `tqdm` progress output from the scraper process.
- `logs\scrape_stdout.log` - normal stdout output.
- `logs\recovery_downloaded.csv` - successful downloads from the recovery scraper.
- `logs\recovery_missing.csv` - recipes the recovery scraper still could not resolve.
- `logs\recovery_failed.csv` - temporary recovery scraper failures.
- `logs\request_throttle.json` - shared request timing and cooldown state.
- `logs\foodcom_recipe_index.csv` - local Food.com recipe title index for exact-match recovery.

## Check Current Progress

Run this from `S:\Image scraper`:

```powershell
venv\Scripts\python.exe status.py
```

Watch it refresh every 30 seconds:

```powershell
venv\Scripts\python.exe status.py --watch 30
```

Quick image count:

```powershell
(Get-ChildItem .\images -File | Where-Object { $_.Name -notlike '.tmp-*' } | Measure-Object).Count
(Get-ChildItem .\recovery_images -File | Where-Object { $_.Name -notlike '.tmp-*' } | Measure-Object).Count
```

Quick log counts:

```powershell
(Import-Csv .\logs\downloaded.csv | Measure-Object).Count
(Import-Csv .\logs\missing.csv | Measure-Object).Count
(Import-Csv .\logs\failed.csv | Measure-Object).Count
```

See the latest downloaded images:

```powershell
Get-ChildItem .\images -File |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 10 Name,Length,LastWriteTime
```

See the latest successful downloads:

```powershell
Get-Content .\logs\downloaded.csv -Tail 10
```

See the latest missing recipes:

```powershell
Get-Content .\logs\missing.csv -Tail 10
```

See live progress output:

```powershell
Get-Content .\logs\scrape_stderr.log -Tail 20 -Wait
```

Check running scraper processes:

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'scraper.py' } |
  Select-Object ProcessId,Name,CommandLine
```

## Run Recovery Scraper

The recovery scraper only targets valid numeric recipe IDs from `logs\missing.csv` and `logs\failed.csv`.
By default, it skips recipe IDs already present in `logs\recovery_downloaded.csv`, `logs\recovery_missing.csv`, or `logs\recovery_failed.csv`, so it does not keep retrying known dead ends.

Start with a small test:

```powershell
venv\Scripts\python.exe recovery_scraper.py --limit 20
```

Run recovery conservatively:

```powershell
venv\Scripts\python.exe recovery_scraper.py --source missing,failed --mode search-first --out recovery_images --delay 8 --concurrency 1
```

Retry only a specific missing reason:

```powershell
venv\Scripts\python.exe recovery_scraper.py --only-reason recipe_page_not_found
```

The recovery scraper uses separate logs, a `logs\recovery_scraper.pid` process lock, and the shared request throttle. If Food.com returns HTTP `429`, it sets a cooldown in `logs\request_throttle.json`.

Build or refresh the local Food.com recipe index manually:

```powershell
venv\Scripts\python.exe recovery_scraper.py --refresh-index --limit 0
```

For a tiny sitemap test instead of a full refresh:

```powershell
venv\Scripts\python.exe recovery_scraper.py --refresh-index --limit-index 1 --limit 0
```

By default, recovery runs `search-first`: it exact-matches the normalized CSV recipe name against the local Food.com sitemap index first, then falls back to URL-based recovery if no exact match exists. It does not scrape Food.com `/search/`.

To intentionally retry recipes already logged in `recovery_missing.csv`, add:

```powershell
--retry-recovery-missing
```

## Resume Behavior

Future runs of `scraper.py` now resume automatically.

By default, the scraper skips recipe IDs already present in:

- `logs\downloaded.csv`
- `logs\missing.csv`
- `logs\failed.csv`
- `logs\skipped_existing.csv`

It also skips valid existing images in `images\`.

Start or resume the main scrape:

```powershell
venv\Scripts\python.exe scraper.py
```

Force a full re-check:

```powershell
venv\Scripts\python.exe scraper.py --no-resume
```

Re-download even existing images:

```powershell
venv\Scripts\python.exe scraper.py --force
```

Retry missing recipes later:

```powershell
venv\Scripts\python.exe scraper.py --only-missing logs\missing.csv
```

Retry failed recipes later:

```powershell
venv\Scripts\python.exe scraper.py --retry-failed logs\failed.csv
```
