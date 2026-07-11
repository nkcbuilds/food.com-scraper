import argparse
import csv
import json
import msvcrt
import os
import random
import re
import shutil
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from threading import Lock

import requests
from bs4 import BeautifulSoup
from PIL import Image
from tqdm import tqdm


class TransientError(Exception):
    """Raised for temporary HTTP errors that warrant a retry/failed log."""
    pass


class InterProcessFileLock:
    def __init__(self, path, timeout=60):
        self.path = Path(path)
        self.timeout = timeout
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+b")
        start = time.time()
        while True:
            try:
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                return self
            except OSError:
                if time.time() - start >= self.timeout:
                    raise TimeoutError(f"Timed out waiting for lock: {self.path}")
                time.sleep(0.2)

    def __exit__(self, exc_type, exc, tb):
        if self.handle:
            self.handle.seek(0)
            try:
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                self.handle.close()
                self.handle = None


def parse_images_field(field):
    """Extract direct http(s) URLs from an R-style c(...) string."""
    if not field or field.strip() in ("character(0)", "NA", ""):
        return []
    urls = re.findall(r'"([^"]+)"', field)
    return [u for u in urls if u.startswith("http")]


def recipe_id_from_image_url(url):
    match = re.search(r"/recipes/((?:\d+/)*\d+)(?:/|[^0-9])", url)
    if not match:
        return None
    return "".join(re.findall(r"\d+", match.group(1)))


class ImageScraper:
    def __init__(self, args):
        self.args = args
        self.images_dir = Path(args.out)
        self.logs_dir = Path(args.logs)
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "FoodComRecipeImageScraper/1.0 "
                "(Local Script; Python requests; Resumable Recipe Image Downloader)"
            )
        })

        self.lock = Lock()
        self.last_request_time = 0.0
        self.request_throttle_lock = self.logs_dir / "request_throttle.lock"
        self.request_throttle_state = self.logs_dir / "request_throttle.json"

        self.stats = {
            "recipes_seen": 0,
            "downloaded": 0,
            "skipped_existing": 0,
            "skipped_already_logged": 0,
            "missing": 0,
            "failed": 0,
        }

        self.started_at = datetime.now(timezone.utc).isoformat()

        self.log_files = {
            "downloaded": self.logs_dir / "downloaded.csv",
            "missing": self.logs_dir / "missing.csv",
            "failed": self.logs_dir / "failed.csv",
            "skipped_existing": self.logs_dir / "skipped_existing.csv",
        }

        self._init_log(
            "downloaded",
            [
                "RecipeId", "Name", "source", "url", "file_path",
                "status_code", "content_type", "bytes", "width", "height", "timestamp",
            ],
        )
        self._init_log(
            "missing",
            ["RecipeId", "Name", "reason", "page_url", "timestamp"],
        )
        self._init_log(
            "failed",
            ["RecipeId", "Name", "source", "url", "status_code", "error", "timestamp"],
        )
        self._init_log(
            "skipped_existing",
            ["RecipeId", "Name", "file_path", "timestamp"],
        )
        self.processed_ids = self._load_processed_ids()

    def _init_log(self, name, headers):
        path = self.log_files[name]
        if not path.exists() or path.stat().st_size == 0:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(headers)

    def _write_log(self, name, row):
        path = self.log_files[name]
        with self.lock:
            with InterProcessFileLock(self.logs_dir / f"{name}.write.lock"):
                with open(path, "a", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(row)

    def _load_processed_ids(self):
        if self.args.no_resume or self.args.force or self.args.only_missing or self.args.retry_failed:
            return set()

        processed = set()
        for name in ("downloaded", "missing", "failed", "skipped_existing"):
            path = self.log_files[name]
            if not path.exists() or path.stat().st_size == 0:
                continue
            try:
                with open(path, "r", newline="", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        recipe_id = row.get("RecipeId")
                        if recipe_id:
                            processed.add(recipe_id)
            except csv.Error:
                continue
        return processed

    def _mark_processed(self, recipe_id):
        if self.args.no_resume or self.args.force or self.args.only_missing or self.args.retry_failed:
            return
        with self.lock:
            self.processed_ids.add(str(recipe_id))

    def _rate_limit(self):
        if self.args.delay <= 0:
            return
        with self.lock:
            while True:
                with InterProcessFileLock(self.request_throttle_lock):
                    state = {}
                    if self.request_throttle_state.exists():
                        try:
                            with open(self.request_throttle_state, "r", encoding="utf-8") as f:
                                state = json.load(f)
                        except (json.JSONDecodeError, OSError):
                            state = {}

                    now = time.time()
                    cooldown_until = float(state.get("cooldown_until", 0) or 0)
                    last_request = max(float(state.get("last_request_at", 0) or 0), self.last_request_time)
                    target_time = max(cooldown_until, last_request + self.args.delay)
                    sleep_time = target_time - now
                    if sleep_time <= 0:
                        self.last_request_time = now
                        state["last_request_at"] = now
                        with open(self.request_throttle_state, "w", encoding="utf-8") as f:
                            json.dump(state, f)
                        return
                time.sleep(min(sleep_time, 60))

    def _request(self, url, retries=None, stream=False):
        if retries is None:
            retries = self.args.retries
        for attempt in range(retries + 1):
            self._rate_limit()
            try:
                resp = self.session.get(url, timeout=self.args.timeout, stream=stream)
                return resp
            except requests.exceptions.RequestException:
                if attempt == retries:
                    raise
                time.sleep(1)
        # Unreachable
        return None

    def _check_existing_image(self, recipe_id):
        if self.args.force:
            return None
        for ext in ("jpg", "jpeg", "png", "webp"):
            path = self.images_dir / f"{recipe_id}.{ext}"
            if path.exists() and path.stat().st_size > 0:
                try:
                    with Image.open(path) as img:
                        img.verify()
                    return path
                except Exception:
                    continue
        return None

    def _validate_and_save(self, recipe_id, name, url, source, response):
        # Treat common transient codes as retryable/failed
        if response.status_code in (429, 500, 502, 503, 504):
            raise TransientError(f"HTTP {response.status_code}")

        if response.status_code != 200:
            raise ValueError(f"HTTP {response.status_code}")

        content_type = response.headers.get("Content-Type", "")
        if not content_type.startswith("image/"):
            raise ValueError(f"Invalid content type: {content_type}")

        content = response.content
        if len(content) < 5120:
            raise ValueError(f"Image too small: {len(content)} bytes")

        try:
            img = Image.open(BytesIO(content))
            img.verify()
            img = Image.open(BytesIO(content))
            width, height = img.size
            fmt = img.format
        except Exception as exc:
            raise ValueError(f"Pillow validation failed: {exc}")

        ext_map = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif"}
        ext = ext_map.get(fmt, "jpg")

        tmp_path = self.images_dir / f".tmp-{recipe_id}"
        final_path = self.images_dir / f"{recipe_id}.{ext}"

        with open(tmp_path, "wb") as f:
            f.write(content)

        shutil.move(str(tmp_path), str(final_path))

        with self.lock:
            self.stats["downloaded"] += 1

        self._write_log(
            "downloaded",
            [
                recipe_id,
                name,
                source,
                url,
                str(final_path),
                response.status_code,
                content_type,
                len(content),
                width,
                height,
                datetime.now(timezone.utc).isoformat(),
            ],
        )
        return final_path

    def _download_csv_image(self, recipe_id, name, urls):
        for url in urls:
            try:
                resp = self._request(url, stream=True)
                return self._validate_and_save(recipe_id, name, url, "csv", resp)
            except (TransientError, requests.exceptions.RequestException, ValueError):
                # Try next URL
                continue
        return None

    @staticmethod
    def _slugify(name):
        name = name.lower()
        name = name.replace("&", "and")
        name = re.sub(r"['\"']", "", name)
        # Remove unsupported punctuation (keep letters, digits, spaces, hyphens)
        name = re.sub(r"[^a-z0-9\s-]+", "", name)
        # Convert remaining non-alphanumeric runs to single hyphens
        name = re.sub(r"[^a-z0-9]+", "-", name)
        name = name.strip("-")
        return name

    def _build_recipe_url(self, recipe_id, name):
        slug = self._slugify(name)
        if slug:
            return f"https://www.food.com/recipe/{slug}-{recipe_id}"
        return f"https://www.food.com/recipe/{recipe_id}"

    def _extract_page_images(self, recipe_id, soup, page_url):
        candidates = []

        # OpenGraph image
        meta = soup.find("meta", property="og:image")
        if meta and meta.get("content"):
            candidates.append(meta["content"])

        # image_src link
        link = soup.find("link", rel="image_src")
        if link and link.get("href"):
            candidates.append(link["href"])

        # img tags tied to this recipe on img.sndimg.com
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src")
            if not src:
                continue
            if "img.sndimg.com" in src:
                # Prefer URLs that contain the recipe id path segment
                if f"/recipes/{recipe_id}/" in src:
                    candidates.append(src)
                elif str(recipe_id) in src:
                    candidates.append(src)

        seen = set()
        unique = []
        for c in candidates:
            image_recipe_id = recipe_id_from_image_url(c)
            if image_recipe_id and image_recipe_id != str(recipe_id):
                continue
            if not image_recipe_id and "gk-static" in c.lower():
                continue
            if c not in seen:
                seen.add(c)
                unique.append(c)
        return unique

    def _download_page_image(self, recipe_id, name, page_url):
        try:
            resp = self._request(page_url)
        except requests.exceptions.RequestException as exc:
            return "failed", str(exc)

        if resp.status_code == 404:
            return "missing", "recipe_page_not_found"
        if resp.status_code in (429, 500, 502, 503, 504):
            return "failed", f"HTTP {resp.status_code}"
        if resp.status_code != 200:
            return "failed", f"HTTP {resp.status_code}"

        soup = BeautifulSoup(resp.text, "html.parser")
        candidates = self._extract_page_images(recipe_id, soup, page_url)

        if not candidates:
            return "missing", "page_has_0_photos"

        for url in candidates:
            try:
                resp_img = self._request(url, stream=True)
                self._validate_and_save(recipe_id, name, url, "food_page", resp_img)
                return "success", None
            except TransientError:
                continue
            except Exception:
                continue

        return "missing", "no_valid_recipe_image"

    def process_recipe(self, recipe_id, name, images_field):
        if str(recipe_id) in self.processed_ids:
            with self.lock:
                self.stats["skipped_already_logged"] += 1
            return

        with self.lock:
            self.stats["recipes_seen"] += 1

        existing = self._check_existing_image(recipe_id)
        if existing:
            with self.lock:
                self.stats["skipped_existing"] += 1
            self._write_log(
                "skipped_existing",
                [
                    recipe_id,
                    name,
                    str(existing),
                    datetime.now(timezone.utc).isoformat(),
                ],
            )
            self._mark_processed(recipe_id)
            return

        urls = parse_images_field(images_field)
        csv_failed = False

        if urls:
            result = self._download_csv_image(recipe_id, name, urls)
            if result:
                self._mark_processed(recipe_id)
                return
            csv_failed = True

        page_url = self._build_recipe_url(recipe_id, name)
        status, reason = self._download_page_image(recipe_id, name, page_url)

        if status == "success":
            self._mark_processed(recipe_id)
            return
        elif status == "failed":
            with self.lock:
                self.stats["failed"] += 1
            self._write_log(
                "failed",
                [
                    recipe_id,
                    name,
                    "food_page",
                    page_url,
                    "",
                    reason,
                    datetime.now(timezone.utc).isoformat(),
                ],
            )
            self._mark_processed(recipe_id)
        elif status == "missing":
            with self.lock:
                self.stats["missing"] += 1
            if reason == "recipe_page_not_found":
                final_reason = "recipe_page_not_found"
            elif reason == "page_has_0_photos":
                final_reason = "csv_images_failed" if csv_failed else "page_has_0_photos"
            elif reason == "no_valid_recipe_image":
                final_reason = "csv_images_failed" if csv_failed else "no_valid_recipe_image"
            else:
                final_reason = reason

            self._write_log(
                "missing",
                [
                    recipe_id,
                    name,
                    final_reason,
                    page_url,
                    datetime.now(timezone.utc).isoformat(),
                ],
            )
            self._mark_processed(recipe_id)

    def _iter_recipes(self):
        if self.args.only_missing:
            with open(self.args.only_missing, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    yield row["RecipeId"], row.get("Name", ""), row.get("Images", "")
        elif self.args.retry_failed:
            with open(self.args.retry_failed, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    yield row["RecipeId"], row.get("Name", ""), ""
        else:
            with open(self.args.csv, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                yielded = 0
                for row in reader:
                    recipe_id = row["RecipeId"]
                    if self.args.start_after is not None and int(recipe_id) <= self.args.start_after:
                        continue
                    yield recipe_id, row["Name"], row.get("Images", "")
                    yielded += 1
                    if self.args.limit is not None and yielded >= self.args.limit:
                        return

    def _handle_future(self, future, future_meta):
        rid, name = future_meta
        try:
            future.result()
        except Exception as exc:
            with self.lock:
                self.stats["failed"] += 1
            self._write_log(
                "failed",
                [
                    rid,
                    name,
                    "unknown",
                    "",
                    "",
                    str(exc),
                    datetime.now(timezone.utc).isoformat(),
                ],
            )
            self._mark_processed(rid)

    def run(self):
        recipe_iter = iter(self._iter_recipes())
        max_pending = max(1, self.args.concurrency * 4)
        total = self.args.limit

        with ThreadPoolExecutor(max_workers=self.args.concurrency) as executor:
            pending = {}

            def submit_next():
                try:
                    rid, name, imgs = next(recipe_iter)
                except StopIteration:
                    return False
                future = executor.submit(self.process_recipe, rid, name, imgs)
                pending[future] = (rid, name)
                return True

            for _ in range(max_pending):
                if not submit_next():
                    break

            with tqdm(total=total, desc="Scraping") as pbar:
                while pending:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        meta = pending.pop(future)
                        self._handle_future(future, meta)
                        pbar.update(1)
                        submit_next()

        self._write_summary()

    def _write_summary(self):
        summary = {
            "started_at": self.started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "csv_path": self.args.csv,
            "images_dir": str(self.images_dir),
            "logs_dir": str(self.logs_dir),
            "recipes_seen": self.stats["recipes_seen"],
            "downloaded": self.stats["downloaded"],
            "skipped_existing": self.stats["skipped_existing"],
            "skipped_already_logged": self.stats["skipped_already_logged"],
            "missing": self.stats["missing"],
            "failed": self.stats["failed"],
        }
        with open(self.logs_dir / "run_summary.json", "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)


def main():
    parser = argparse.ArgumentParser(description="Food.com Recipe Image Scraper")
    parser.add_argument("--csv", default="recipes.csv", help="Path to recipes CSV")
    parser.add_argument("--out", default="images", help="Output images directory")
    parser.add_argument("--logs", default="logs", help="Logs directory")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of recipes")
    parser.add_argument("--start-after", type=int, default=None, help="Start after RecipeId")
    parser.add_argument("--only-missing", default=None, help="Retry recipes from missing log")
    parser.add_argument("--retry-failed", default=None, help="Retry recipes from failed log")
    parser.add_argument("--force", action="store_true", help="Re-download existing images")
    parser.add_argument("--no-resume", action="store_true", help="Do not skip recipe IDs already present in logs")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between requests (seconds)")
    parser.add_argument("--concurrency", type=int, default=4, help="Concurrent workers")
    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout (seconds)")
    parser.add_argument("--retries", type=int, default=3, help="Retries per request")
    args = parser.parse_args()

    scraper = ImageScraper(args)
    scraper.run()


if __name__ == "__main__":
    main()
