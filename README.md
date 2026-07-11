# food.com-scraper

A Python scraper for food.com recipes and images.

## Files

- `scraper.py` — main scraper (sitemap → recipe pages → CSV + image download).
- `recovery_scraper.py` — recovers failed/partial recipes from logs.
- `status.py` — prints progress and aggregate stats.
- `requirements.txt` — Python dependencies.

## Setup

```bash
python -m venv venv
# Windows
venv\Scripts\activate
pip install -r requirements.txt
```

## Usage

```bash
python scraper.py
python status.py
```

See `SCRAPER_COMMANDS.md` for the full command reference.
