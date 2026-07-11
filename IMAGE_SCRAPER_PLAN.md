# Food.com Recipe Image Scraper Plan

## Current Repository State

The repository currently contains one data file:

- `recipes.csv` - a large recipe dataset, approximately 705 MB.

The CSV has these relevant columns:

- `RecipeId` - the stable identifier that should be used as the image filename.
- `Name` - the recipe title, useful for constructing Food.com recipe URLs.
- `Images` - an R-style image list, usually containing direct `img.sndimg.com` image URLs, or `character(0)` when no image is listed.

Initial inspection found:

- Total recipes: `522,568`
- Rows with non-empty image data: `165,938`
- Rows with empty image data: `356,630`
- Total direct image URLs found in populated rows: `408,769`

Example Food.com page URL pattern:

```text
https://www.food.com/recipe/low-fat-berry-blue-frozen-dessert-38
```

The URL appears to be:

```text
https://www.food.com/recipe/{slugified-recipe-name}-{RecipeId}
```

## Goal

Create a resumable scraper that downloads accurate recipe images from Food.com or its image CDN and stores them locally using the recipe ID as the filename.

The core output should be:

```text
images/
  38.jpg
  39.jpg
  40.jpg
```

Each image should be referenceable later by `RecipeId`.

Recipes whose images are not found or cannot be downloaded should be recorded in retryable log files, so future runs can attempt them again.

## Chosen Behavior

The scraper should save one image per recipe.

When multiple images are available, it should download the first valid image. This keeps the output simple and directly matches the requirement that the filename be the recipe ID.

The scraper should use this source order:

1. Direct image URLs already present in the CSV `Images` column.
2. Food.com recipe page fallback for rows with no CSV image or broken CSV images.
3. If no valid image is found on the recipe page, record the recipe as missing.

No broad web search fallback should be used in the first version, because it increases the risk of inaccurate images.

## Proposed Files

The implementation should eventually add:

```text
scraper.py
requirements.txt
IMAGE_SCRAPER_PLAN.md
images/
logs/
```

Planned runtime outputs:

```text
images/
  {RecipeId}.{ext}

logs/
  downloaded.csv
  missing.csv
  failed.csv
  skipped_existing.csv
  run_summary.json
```

This plan file is the only file being added now. The scraper itself is not implemented yet.

## Scraper Workflow

### 1. Stream the CSV

The scraper should read `recipes.csv` row by row using Python's standard `csv` module.

It should not load the full CSV into memory.

For each row, read:

- `RecipeId`
- `Name`
- `Images`

### 2. Skip Existing Valid Images

Before downloading, check whether an image already exists for the recipe:

```text
images/{RecipeId}.jpg
images/{RecipeId}.jpeg
images/{RecipeId}.png
images/{RecipeId}.webp
```

If a valid existing image is found, skip the recipe and log it to `logs/skipped_existing.csv`.

A `--force` option should allow re-downloading existing images.

### 3. Parse Existing CSV Image URLs

The `Images` column uses an R-style format:

```text
c("https://img.sndimg.com/...", "https://img.sndimg.com/...")
```

The parser should extract all `http` and `https` URLs from this field.

The parser should treat these values as empty:

```text
character(0)
NA

```

### 4. Download the First Valid CSV Image

For each extracted CSV image URL:

1. Send an HTTP request with timeout and retries.
2. Confirm the response status is `200`.
3. Confirm `Content-Type` starts with `image/`.
4. Confirm the response is larger than a minimum size, such as `5 KB`.
5. Open the downloaded bytes with Pillow to confirm it is a real image.
6. Save it as:

```text
images/{RecipeId}.{detected_extension}
```

If a CSV image succeeds, do not fetch the Food.com recipe page.

### 5. Build Food.com Recipe Page URL

If no CSV image exists or all CSV image URLs fail, build the Food.com recipe page URL.

Slug rules:

- Lowercase the recipe name.
- Replace `&` with `and`.
- Remove apostrophes and unsupported punctuation.
- Convert remaining non-alphanumeric runs to single hyphens.
- Trim leading and trailing hyphens.
- Append `-{RecipeId}`.

Example:

```text
Low-Fat Berry Blue Frozen Dessert + 38
```

becomes:

```text
https://www.food.com/recipe/low-fat-berry-blue-frozen-dessert-38
```

### 6. Extract Recipe Image URLs from the Page

Fetch the Food.com recipe page only as fallback.

Extract image candidates from reliable page locations:

- recipe photo gallery links
- image metadata
- `img.sndimg.com/food/image/upload/.../recipes/{RecipeId}/...` URLs

Reject:

- placeholder images
- "Add your photo" images
- author profile images
- advertisement images
- related-recipe thumbnails
- any image URL that does not appear tied to the current `RecipeId`

Then validate and download the first valid image using the same validation flow as CSV image URLs.

### 7. Record Missing Recipes

If no valid image is found, log the recipe to `logs/missing.csv`.

Suggested columns:

```text
RecipeId,Name,reason,page_url,timestamp
```

Suggested reasons:

- `no_csv_image`
- `csv_images_failed`
- `recipe_page_not_found`
- `page_has_0_photos`
- `no_valid_recipe_image`

### 8. Record Failed Downloads

If a recipe might succeed later because of network or server issues, log it to `logs/failed.csv`.

Suggested columns:

```text
RecipeId,Name,source,url,status_code,error,timestamp
```

Examples:

- timeout
- connection reset
- temporary HTTP `429`
- temporary HTTP `500`
- invalid partial download

### 9. Record Successful Downloads

Log successful downloads to `logs/downloaded.csv`.

Suggested columns:

```text
RecipeId,Name,source,url,file_path,status_code,content_type,bytes,width,height,timestamp
```

`source` should be one of:

- `csv`
- `food_page`

### 10. Write Run Summary

At the end of each run, write `logs/run_summary.json`.

Suggested fields:

```json
{
  "started_at": "2026-05-05T00:00:00Z",
  "finished_at": "2026-05-05T00:00:00Z",
  "csv_path": "recipes.csv",
  "images_dir": "images",
  "recipes_seen": 0,
  "downloaded": 0,
  "skipped_existing": 0,
  "missing": 0,
  "failed": 0
}
```

## Command Line Interface

The scraper should support:

```text
python scraper.py --csv recipes.csv --out images --logs logs
```

Useful options:

```text
--limit 100
--start-after 12345
--only-missing logs/missing.csv
--retry-failed logs/failed.csv
--force
--delay 1.0
--concurrency 4
--timeout 20
--retries 3
```

Recommended defaults:

- `--delay 1.0`
- `--concurrency 4`
- `--timeout 20`
- `--retries 3`
- minimum image size: `5 KB`

## Resumability

The scraper should be safe to stop and rerun.

On each run:

1. Check whether an image already exists for each `RecipeId`.
2. Skip valid existing images.
3. Append logs incrementally.
4. Use temporary files while downloading.
5. Rename the temporary file only after validation passes.

Temporary download pattern:

```text
images/.tmp-{RecipeId}
images/{RecipeId}.{ext}
```

This prevents corrupt partial files from being mistaken as completed images.

## Accuracy Rules

The scraper should prioritize accuracy over coverage.

For version one:

- Use direct Food.com/CDN image URLs when available.
- Use only Food.com recipe pages as fallback.
- Avoid Google/Bing/general web image search.
- Avoid downloading related-recipe images.
- Avoid downloading generic placeholder images.
- Prefer a missing log entry over saving a questionable image.

## Politeness and Site Handling

The scraper should:

- Use a clear User-Agent string identifying the local scraper.
- Respect timeouts and retry limits.
- Use conservative concurrency.
- Avoid hammering Food.com recipe pages.
- Prefer direct CDN image downloads from URLs already present in the CSV.

Food.com `robots.txt` should be checked during implementation and respected.

## Testing Plan

### Unit Tests

Test CSV image parsing:

- `c("url1", "url2")` returns both URLs.
- `character(0)` returns no URLs.
- empty strings return no URLs.
- malformed image fields do not crash the scraper.

Test slug generation:

- `Low-Fat Berry Blue Frozen Dessert` becomes `low-fat-berry-blue-frozen-dessert`.
- names with apostrophes, ampersands, commas, slashes, and extra spaces normalize correctly.
- final URL always ends with `-{RecipeId}`.

Test image validation:

- valid JPEG passes.
- HTML response pretending to be an image fails.
- tiny placeholder-like files fail.
- corrupt image bytes fail.

### Smoke Tests

Run:

```text
python scraper.py --limit 20
```

Verify:

- images are created in `images/`
- filenames are recipe IDs
- logs are created
- rerunning skips existing images

Run:

```text
python scraper.py --limit 100
```

Verify:

- CSV image rows download from direct URLs.
- empty image rows try Food.com page fallback.
- recipes with no page image are recorded in `missing.csv`.

### Retry Tests

Run:

```text
python scraper.py --only-missing logs/missing.csv
```

and:

```text
python scraper.py --retry-failed logs/failed.csv
```

Verify:

- only selected recipe IDs are retried.
- logs remain append-only and readable.
- existing valid images are not duplicated.

## Implementation Notes

Recommended Python packages:

- `requests` or `httpx` for HTTP
- `beautifulsoup4` for Food.com HTML parsing
- `Pillow` for image validation
- `tqdm` for progress display

The implementation should stay simple. A single `scraper.py` file is enough for the first version unless the code grows too large.

## Acceptance Criteria

The scraper is complete when:

- It can stream `recipes.csv` without memory issues.
- It downloads valid images using `RecipeId` filenames.
- It uses CSV image URLs before Food.com page fallback.
- It records successful downloads.
- It records missing recipes.
- It records failed transient downloads.
- It can resume safely after interruption.
- It can retry missing or failed recipes.
- It does not modify `recipes.csv`.

