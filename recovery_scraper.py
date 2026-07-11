import argparse
import csv
import gzip
import html
import json
import math
import msvcrt
import os
import random
import re
import shutil
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from threading import Lock
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup
from PIL import Image
from tqdm import tqdm


TRANSIENT_CODES = {429, 500, 502, 503, 504}
IMAGE_EXTENSIONS = ("jpg", "jpeg", "png", "webp", "gif")
SITEMAP_INDEX_URL = "https://www.food.com/sitemap.xml"
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


class TransientError(Exception):
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


class SharedThrottle:
    def __init__(self, logs_dir, jitter):
        self.logs_dir = Path(logs_dir)
        self.lock_path = self.logs_dir / "request_throttle.lock"
        self.state_path = self.logs_dir / "request_throttle.json"
        self.jitter = jitter

    def wait(self, delay):
        if delay <= 0:
            return
        while True:
            with InterProcessFileLock(self.lock_path):
                state = self._read_state()
                now = time.time()
                cooldown_until = float(state.get("cooldown_until", 0) or 0)
                last_request = float(state.get("last_request_at", 0) or 0)
                jitter = random.uniform(0, self.jitter) if self.jitter > 0 else 0
                target_time = max(cooldown_until, last_request + delay + jitter)
                sleep_time = target_time - now
                if sleep_time <= 0:
                    state["last_request_at"] = now
                    self._write_state(state)
                    return
            time.sleep(min(sleep_time, 60))

    def cooldown(self, seconds, reason):
        with InterProcessFileLock(self.lock_path):
            state = self._read_state()
            until = time.time() + seconds
            state["cooldown_until"] = max(float(state.get("cooldown_until", 0) or 0), until)
            state["cooldown_reason"] = reason
            state["cooldown_set_at"] = datetime.now(timezone.utc).isoformat()
            self._write_state(state)

    def _read_state(self):
        if not self.state_path.exists():
            return {}
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_state(self, state):
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def normalize_text(value):
    value = html.unescape(value or "")
    value = unquote(value)
    value = value.lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def clean_foodcom_title(value):
    value = html.unescape(value or "")
    value = re.sub(r"\s+-\s+Food\.com\s*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+Recipe\s*$", "", value, flags=re.IGNORECASE)
    return value.strip()


def title_from_recipe_slug(url):
    path = urlparse(url).path.rstrip("/")
    slug = path.rsplit("/", 1)[-1]
    match = re.match(r"(.+)-(\d+)$", slug)
    if not match:
        return None, None
    title_slug, recipe_id = match.groups()
    title = title_slug.replace("-", " ")
    return recipe_id, html.unescape(title)


def is_recipe_url(url):
    parsed = urlparse(url)
    if parsed.netloc.lower() != "www.food.com":
        return False
    return bool(re.match(r"^/recipe/.+-\d+/?$", parsed.path))


def token_similarity(a, b):
    a_tokens = {t for t in normalize_text(a).split() if len(t) > 1}
    b_tokens = {t for t in normalize_text(b).split() if len(t) > 1}
    if not a_tokens or not b_tokens:
        return 0.0
    return len(a_tokens & b_tokens) / math.sqrt(len(a_tokens) * len(b_tokens))


def slugify(name, ampersand_mode):
    name = html.unescape(name or "")
    if ampersand_mode == "and":
        name = name.replace("&", " and ")
    elif ampersand_mode == "drop":
        name = name.replace("&", " ")
    else:
        name = name.replace("&", " ")
    name = name.lower()
    name = re.sub(r"['\"]", "", name)
    name = re.sub(r"[^a-z0-9\s-]+", " ", name)
    name = re.sub(r"[^a-z0-9]+", "-", name)
    return name.strip("-")


def build_candidate_page_urls(recipe_id, name, existing_url):
    urls = []
    if existing_url and existing_url.startswith("http"):
        urls.append(existing_url)

    for mode in ("drop", "and"):
        slug = slugify(name, mode)
        if slug:
            urls.append(f"https://www.food.com/recipe/{slug}-{recipe_id}")

    urls.append(f"https://www.food.com/recipe/{recipe_id}")

    unique = []
    seen = set()
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def recipe_id_from_image_url(url):
    match = re.search(r"/recipes/((?:\d+/)*\d+)(?:/|[^0-9])", url)
    if not match:
        return None
    return "".join(re.findall(r"\d+", match.group(1)))


def parse_sitemap_locs(xml_bytes):
    root = ET.fromstring(xml_bytes)
    return [
        loc.text.strip()
        for loc in root.findall(".//sm:loc", SITEMAP_NS)
        if loc.text and loc.text.strip()
    ]


def parse_recovery_inputs(logs_dir, source_names, only_reason):
    logs_dir = Path(logs_dir)
    candidates = {}

    def add(row, source):
        recipe_id = (row.get("RecipeId") or row.get("\ufeffRecipeId") or "").strip()
        if not recipe_id.isdigit():
            return
        reason = (row.get("reason") or row.get("error") or "").strip()
        if only_reason and reason != only_reason:
            return
        if recipe_id not in candidates:
            candidates[recipe_id] = {
                "RecipeId": recipe_id,
                "Name": html.unescape((row.get("Name") or "").strip()),
                "reason": reason,
                "page_url": (row.get("page_url") or row.get("url") or "").strip(),
                "source_log": source,
            }

    if "missing" in source_names:
        path = logs_dir / "missing.csv"
        if path.exists():
            with open(path, "r", newline="", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    add(row, "missing")

    if "failed" in source_names:
        path = logs_dir / "failed.csv"
        if path.exists():
            with open(path, "r", newline="", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    add(row, "failed")

    return list(candidates.values())


class RecoveryScraper:
    def __init__(self, args):
        self.args = args
        self.images_dir = Path(args.out)
        self.primary_images_dir = Path(args.primary_images)
        self.logs_dir = Path(args.logs)
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.primary_images_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "FoodComRecipeRecoveryScraper/1.0 "
                "(Local Script; Conservative Missing Recipe Image Recovery)"
            )
        })

        self.lock = Lock()
        self.throttle = SharedThrottle(self.logs_dir, args.jitter)
        self.started_at = utc_now()
        self.stats = {
            "seen": 0,
            "downloaded": 0,
            "skipped_existing": 0,
            "skipped_already_logged": 0,
            "missing": 0,
            "failed": 0,
            "ignored_bad_rows": 0,
        }

        self.log_files = {
            "downloaded": self.logs_dir / "recovery_downloaded.csv",
            "missing": self.logs_dir / "recovery_missing.csv",
            "failed": self.logs_dir / "recovery_failed.csv",
        }
        self.log_headers = {
            "downloaded": [
                "RecipeId", "Name", "match_mode", "match_status", "matched_title", "matched_url",
                "page_url", "image_url", "file_path", "status_code", "content_type",
                "bytes", "width", "height", "match_score", "timestamp",
            ],
            "missing": [
                "RecipeId", "Name", "match_mode", "match_status", "source_log",
                "original_reason", "reason", "matched_title", "matched_url",
                "attempted_urls", "timestamp",
            ],
            "failed": [
                "RecipeId", "Name", "match_mode", "match_status", "source",
                "url", "status_code", "error", "timestamp",
            ],
        }
        self._init_log("downloaded", self.log_headers["downloaded"])
        self._init_log("missing", self.log_headers["missing"])
        self._init_log("failed", self.log_headers["failed"])
        self.processed_ids = self._load_recovery_processed_ids()
        self.index = {}
        self.index_row_count = 0

    def _init_log(self, name, headers):
        path = self.log_files[name]
        with InterProcessFileLock(self.logs_dir / f"recovery_{name}.write.lock"):
            if path.exists() and path.stat().st_size > 0:
                try:
                    with open(path, "r", newline="", encoding="utf-8-sig", errors="replace") as f:
                        existing_header = next(csv.reader(f), [])
                except (OSError, csv.Error):
                    existing_header = []
                if existing_header and existing_header != headers:
                    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                    legacy_path = path.with_name(f"{path.stem}.legacy-{stamp}{path.suffix}")
                    shutil.move(str(path), str(legacy_path))
            if not path.exists() or path.stat().st_size == 0:
                with open(path, "w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(headers)

    def _write_log(self, name, row):
        path = self.log_files[name]
        with self.lock:
            with InterProcessFileLock(self.logs_dir / f"recovery_{name}.write.lock"):
                with open(path, "a", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(row)

    def _load_recovery_processed_ids(self):
        if self.args.no_resume or self.args.force:
            return set()
        processed = set()
        names = ["downloaded", "failed"]
        if not self.args.retry_recovery_missing:
            names.append("missing")
        for name in names:
            path = self.log_files[name]
            if not path.exists() or path.stat().st_size == 0:
                continue
            with open(path, "r", newline="", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    recipe_id = row.get("RecipeId") or row.get("\ufeffRecipeId")
                    if recipe_id and recipe_id.isdigit():
                        processed.add(recipe_id)
        return processed

    def _existing_image(self, recipe_id):
        if self.args.force:
            return None
        for directory in (self.primary_images_dir, self.images_dir):
            for ext in IMAGE_EXTENSIONS:
                path = directory / f"{recipe_id}.{ext}"
                if not path.exists() or path.stat().st_size <= 0:
                    continue
                try:
                    with Image.open(path) as img:
                        img.verify()
                    return path
                except Exception:
                    continue
        return None

    def _request(self, url, kind):
        if kind == "image":
            delay = self.args.cdn_delay
        elif kind == "sitemap":
            delay = self.args.index_delay
        else:
            delay = self.args.delay
        last_error = None
        for attempt in range(self.args.retries + 1):
            self.throttle.wait(delay)
            try:
                resp = self.session.get(url, timeout=self.args.timeout)
            except requests.exceptions.RequestException as exc:
                last_error = exc
                if attempt == self.args.retries:
                    raise
                time.sleep(min(60, 2 ** attempt))
                continue

            if resp.status_code == 429:
                self.throttle.cooldown(self.args.cooldown, "HTTP 429")
                if attempt == self.args.retries:
                    return resp
                continue

            if resp.status_code in TRANSIENT_CODES and attempt < self.args.retries:
                time.sleep(min(60, 2 ** attempt))
                continue

            return resp

        if last_error:
            raise last_error
        raise TransientError("request failed without response")

    def _refresh_index(self):
        index_path = Path(self.args.index_path)
        index_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = index_path.with_suffix(index_path.suffix + ".tmp")

        index_resp = self._request(SITEMAP_INDEX_URL, "sitemap")
        if index_resp.status_code != 200:
            raise RuntimeError(f"Could not fetch sitemap index: HTTP {index_resp.status_code}")

        sitemap_urls = [
            url for url in parse_sitemap_locs(index_resp.content)
            if url.startswith("https://www.food.com/sitemap-") and url.endswith(".xml.gz")
        ]
        if self.args.limit_index is not None:
            sitemap_urls = sitemap_urls[: self.args.limit_index]

        rows_written = 0
        with open(tmp_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["RecipeId", "title_from_slug", "normalized_title", "url"])
            for sitemap_url in tqdm(sitemap_urls, desc="Indexing Food.com sitemaps"):
                resp = self._request(sitemap_url, "sitemap")
                if resp.status_code == 429:
                    raise RuntimeError("HTTP 429 while refreshing Food.com index")
                if resp.status_code != 200:
                    self._write_log("failed", [
                        "", "", "index_refresh", "sitemap_fetch_failed",
                        "sitemap", sitemap_url, resp.status_code,
                        f"HTTP {resp.status_code}", utc_now(),
                    ])
                    continue
                try:
                    xml_bytes = gzip.decompress(resp.content)
                except OSError:
                    xml_bytes = resp.content

                for url in parse_sitemap_locs(xml_bytes):
                    if not is_recipe_url(url):
                        continue
                    recipe_id, title = title_from_recipe_slug(url)
                    if not recipe_id or not title:
                        continue
                    writer.writerow([recipe_id, title, normalize_text(title), url])
                    rows_written += 1

        shutil.move(str(tmp_path), str(index_path))
        return rows_written

    def _load_index(self):
        index_path = Path(self.args.index_path)
        if not index_path.exists() or index_path.stat().st_size == 0:
            return {}

        index = {}
        rows = 0
        with open(index_path, "r", newline="", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows += 1
                normalized = (row.get("normalized_title") or "").strip()
                url = (row.get("url") or "").strip()
                title = (row.get("title_from_slug") or "").strip()
                recipe_id = (row.get("RecipeId") or "").strip()
                if not normalized or not url:
                    continue
                index.setdefault(normalized, []).append({
                    "RecipeId": recipe_id,
                    "title": title,
                    "url": url,
                })
        self.index_row_count = rows
        return index

    def _find_exact_index_match(self, name):
        normalized = normalize_text(name)
        matches = self.index.get(normalized, [])
        unique = {}
        for match in matches:
            unique[match["url"]] = match
        matches = list(unique.values())
        if len(matches) == 1:
            return "exact_match", matches[0]
        if len(matches) > 1:
            return "ambiguous_exact_match", matches
        return "no_exact_name_match", None

    def _page_matches_recipe(self, recipe_id, name, page_url, soup):
        title = soup.find("title")
        title_text = title.get_text(" ", strip=True) if title else ""
        meta_title = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name": "title"})
        if meta_title and meta_title.get("content"):
            title_text = meta_title["content"] or title_text
        cleaned = clean_foodcom_title(title_text)
        match = normalize_text(name) == normalize_text(cleaned)
        return (1.0 if match else 0.0), cleaned

    def _try_page_for_image(self, item, page_url, match_mode, match_status, matched_title, matched_url):
        recipe_id = item["RecipeId"]
        name = item["Name"]
        try:
            page = self._request(page_url, "page")
        except Exception as exc:
            return "failed", str(exc)

        if page.status_code == 404:
            return "missing", "recipe_page_not_found"
        if page.status_code in TRANSIENT_CODES:
            return "failed", f"HTTP {page.status_code}"
        if page.status_code != 200:
            return "failed", f"HTTP {page.status_code}"

        soup = BeautifulSoup(page.text, "html.parser")
        match_score, page_title = self._page_matches_recipe(recipe_id, name, page.url, soup)
        if match_score < 1:
            return "missing", f"page_title_not_exact:{page_title}"

        candidates = self._extract_image_candidates(recipe_id, soup)
        if not candidates:
            return "missing", "no_image_candidates"

        for image_url in candidates:
            try:
                image = self._request(image_url, "image")
                self._validate_and_save(
                    recipe_id, name, match_mode, match_status,
                    matched_title or page_title, matched_url or page.url,
                    page.url, image_url, image, match_score,
                )
                return "success", None
            except TransientError as exc:
                last_error = str(exc)
                continue
            except Exception as exc:
                last_error = str(exc)
                continue

        return "missing", last_error if "last_error" in locals() else "no_valid_recipe_image"

    def _extract_image_candidates(self, recipe_id, soup):
        raw_candidates = []

        for meta in soup.find_all("meta"):
            prop = " ".join(filter(None, [meta.get("property"), meta.get("name")])).lower()
            content = meta.get("content")
            if content and "image" in prop:
                raw_candidates.append(content)

        for link in soup.find_all("link"):
            rel = " ".join(link.get("rel") or []).lower()
            href = link.get("href")
            if href and "image" in rel:
                raw_candidates.append(href)

        for img in soup.find_all("img"):
            for attr in ("src", "data-src", "data-lazy-src", "srcset", "data-srcset"):
                value = img.get(attr)
                if not value:
                    continue
                raw_candidates.extend(part.strip().split(" ")[0] for part in value.split(","))

        page_text = str(soup)
        page_text = html.unescape(page_text).replace("\\/", "/")
        raw_candidates.extend(re.findall(r"https?://img\.sndimg\.com[^\"'\\<>\s)]+", page_text))

        candidates = []
        seen = set()
        for candidate in raw_candidates:
            url = html.unescape(candidate or "").replace("\\/", "/")
            url = url.strip().strip('"').strip("'")
            if not url.startswith("http") or "img.sndimg.com" not in url:
                continue
            lowered = url.lower()
            if any(bad in lowered for bad in (
                "avatar", "profile", "placeholder", "missing", "add-photo",
                "sharegraphic", "gk-static", "logo", "sprite",
            )):
                continue
            image_recipe_id = recipe_id_from_image_url(url)
            if self.args.image_match == "strict" and image_recipe_id != str(recipe_id):
                continue
            score = 3 if image_recipe_id == str(recipe_id) else 1
            if "/img/feed/" in lowered:
                score = max(score, 2)
            if url not in seen:
                seen.add(url)
                candidates.append((score, url))

        candidates.sort(key=lambda item: item[0], reverse=True)
        return [url for _, url in candidates]

    def _validate_and_save(
        self, recipe_id, name, match_mode, match_status, matched_title,
        matched_url, page_url, image_url, response, match_score,
    ):
        if response.status_code in TRANSIENT_CODES:
            raise TransientError(f"HTTP {response.status_code}")
        if response.status_code != 200:
            raise ValueError(f"HTTP {response.status_code}")

        content_type = response.headers.get("Content-Type", "")
        if not content_type.startswith("image/"):
            raise ValueError(f"Invalid content type: {content_type}")

        content = response.content
        if len(content) < self.args.min_bytes:
            raise ValueError(f"Image too small: {len(content)} bytes")

        try:
            img = Image.open(BytesIO(content))
            img.verify()
            img = Image.open(BytesIO(content))
            width, height = img.size
            fmt = img.format
        except Exception as exc:
            raise ValueError(f"Pillow validation failed: {exc}")

        ext = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif"}.get(fmt, "jpg")
        tmp_path = self.images_dir / f".tmp-recovery-{recipe_id}-{os.getpid()}"
        final_path = self.images_dir / f"{recipe_id}.{ext}"

        with open(tmp_path, "wb") as f:
            f.write(content)
        shutil.move(str(tmp_path), str(final_path))

        self._write_log("downloaded", [
            recipe_id, name, match_mode, match_status, matched_title, matched_url,
            page_url, image_url, str(final_path), response.status_code, content_type,
            len(content), width, height, f"{match_score:.3f}", utc_now(),
        ])

    def _recover_one(self, item):
        recipe_id = item["RecipeId"]
        name = item["Name"]

        if recipe_id in self.processed_ids:
            with self.lock:
                self.stats["skipped_already_logged"] += 1
            return

        with self.lock:
            self.stats["seen"] += 1

        existing = self._existing_image(recipe_id)
        if existing:
            with self.lock:
                self.stats["skipped_existing"] += 1
            return

        attempted = []
        last_error = None
        match_mode = ""
        match_status = ""
        matched_title = ""
        matched_url = ""

        if self.args.mode in ("search-first", "search-only"):
            match_status, match = self._find_exact_index_match(name)
            match_mode = "search_index"
            if match_status == "exact_match":
                matched_title = match["title"]
                matched_url = match["url"]
                attempted.append(matched_url)
                status, reason = self._try_page_for_image(
                    item, matched_url, match_mode, match_status, matched_title, matched_url,
                )
                if status == "success":
                    with self.lock:
                        self.stats["downloaded"] += 1
                        self.processed_ids.add(recipe_id)
                    return
                if status == "failed":
                    last_error = reason
                else:
                    last_error = "exact_match_no_image" if reason == "no_image_candidates" else reason
            elif match_status == "ambiguous_exact_match":
                reason = "ambiguous_exact_match"
                matched_title = name
                matched_url = json.dumps([m["url"] for m in match])
                with self.lock:
                    self.stats["missing"] += 1
                    self.processed_ids.add(recipe_id)
                self._write_log("missing", [
                    recipe_id, name, match_mode, match_status, item.get("source_log", ""),
                    item.get("reason", ""), reason, matched_title, matched_url,
                    json.dumps(attempted), utc_now(),
                ])
                return
            else:
                last_error = "no_exact_name_match"

            if self.args.mode == "search-only" or match_status in ("exact_match", "ambiguous_exact_match"):
                reason = last_error or match_status
                with self.lock:
                    self.stats["missing"] += 1
                    self.processed_ids.add(recipe_id)
                self._write_log("missing", [
                    recipe_id, name, match_mode, match_status, item.get("source_log", ""),
                    item.get("reason", ""), reason, matched_title, matched_url,
                    json.dumps(attempted), utc_now(),
                ])
                return

        if self.args.mode in ("search-first", "url-only"):
            match_mode = "url_fallback"
            match_status = "url_candidate"
            urls = build_candidate_page_urls(recipe_id, name, item.get("page_url"))
            if item.get("reason") == "page_has_0_photos" and urls:
                urls = [u for u in urls if u != item.get("page_url")]

            for page_url in urls:
                attempted.append(page_url)
                status, reason = self._try_page_for_image(
                    item, page_url, match_mode, match_status, "", "",
                )
                if status == "success":
                    with self.lock:
                        self.stats["downloaded"] += 1
                        self.processed_ids.add(recipe_id)
                    return
                last_error = "url_fallback_no_image" if reason == "no_image_candidates" else reason
                if status == "failed" and (last_error.startswith("HTTP 429") or last_error.startswith("HTTP 5")):
                    break

        reason = last_error or "no_valid_recipe_image"
        if reason.startswith("HTTP 429") or reason.startswith("HTTP 5"):
            with self.lock:
                self.stats["failed"] += 1
            self._write_log("failed", [
                recipe_id, name, match_mode, match_status, "food_page_recovery",
                attempted[-1] if attempted else "", "", reason, utc_now(),
            ])
        else:
            with self.lock:
                self.stats["missing"] += 1
            self._write_log("missing", [
                recipe_id, name, match_mode, match_status, item.get("source_log", ""),
                item.get("reason", ""), reason, matched_title, matched_url,
                json.dumps(attempted), utc_now(),
            ])
        with self.lock:
            self.processed_ids.add(recipe_id)

    def run(self):
        if self.args.refresh_index:
            self.index_row_count = self._refresh_index()
        self.index = self._load_index()

        sources = {s.strip() for s in self.args.source.split(",") if s.strip()}
        items = parse_recovery_inputs(self.logs_dir, sources, self.args.only_reason)
        valid_input_count = len(items)
        if self.args.limit is not None:
            items = items[: self.args.limit]

        max_pending = max(1, self.args.concurrency * 2)
        item_iter = iter(items)
        with ThreadPoolExecutor(max_workers=self.args.concurrency) as executor:
            pending = {}

            def submit_next():
                try:
                    item = next(item_iter)
                except StopIteration:
                    return False
                future = executor.submit(self._recover_one, item)
                pending[future] = item
                return True

            for _ in range(max_pending):
                if not submit_next():
                    break

            with tqdm(total=len(items), desc="Recovering") as pbar:
                while pending:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        item = pending.pop(future)
                        try:
                            future.result()
                        except Exception as exc:
                            with self.lock:
                                self.stats["failed"] += 1
                            self._write_log("failed", [
                                item["RecipeId"], item["Name"], "unknown", "exception",
                                "unknown", "", "", str(exc), utc_now(),
                            ])
                        pbar.update(1)
                        submit_next()

        self._write_summary(valid_input_count, len(items))

    def _write_summary(self, valid_input_count, selected_count):
        summary = {
            "started_at": self.started_at,
            "finished_at": utc_now(),
            "sources": self.args.source,
            "valid_input_count": valid_input_count,
            "selected_count": selected_count,
            "images_dir": str(self.images_dir),
            "primary_images_dir": str(self.primary_images_dir),
            "logs_dir": str(self.logs_dir),
            "image_match": self.args.image_match,
            "mode": self.args.mode,
            "index_path": self.args.index_path,
            "index_row_count": self.index_row_count,
            **self.stats,
        }
        with InterProcessFileLock(self.logs_dir / "recovery_summary.write.lock"):
            with open(self.logs_dir / "recovery_summary.json", "w", encoding="utf-8") as f:
                json.dump(summary, f, indent=2)


def acquire_pid_lock(path):
    lock = InterProcessFileLock(path, timeout=1)
    lock.__enter__()
    lock.handle.seek(0)
    lock.handle.truncate()
    lock.handle.write(str(os.getpid()).encode("utf-8"))
    lock.handle.flush()
    return lock


def main():
    parser = argparse.ArgumentParser(description="Recover images for missing/failed Food.com recipes.")
    parser.add_argument("--source", default="missing,failed", help="Comma-separated source logs: missing,failed")
    parser.add_argument("--mode", choices=("search-first", "search-only", "url-only"), default="search-first", help="Recovery mode")
    parser.add_argument("--refresh-index", action="store_true", help="Refresh local Food.com recipe index before recovery")
    parser.add_argument("--index-path", default="logs\\foodcom_recipe_index.csv", help="Local Food.com recipe index path")
    parser.add_argument("--limit-index", type=int, default=None, help="Limit sitemap files while refreshing index")
    parser.add_argument("--out", default="recovery_images", help="Output directory for recovery images")
    parser.add_argument("--primary-images", default="images", help="Main scraper image directory to check before recovery")
    parser.add_argument("--logs", default="logs", help="Logs directory")
    parser.add_argument("--limit", type=int, default=None, help="Limit recovery attempts")
    parser.add_argument("--only-reason", default=None, help="Only recover rows with this missing/error reason")
    parser.add_argument("--force", action="store_true", help="Re-download even if an image exists")
    parser.add_argument("--no-resume", action="store_true", help="Do not skip IDs already in recovery logs")
    parser.add_argument("--retry-recovery-missing", action="store_true", help="Retry IDs already logged in recovery_missing.csv")
    parser.add_argument("--image-match", choices=("name", "strict"), default="name", help="Use page name matching or strict same-ID image URL matching")
    parser.add_argument("--delay", type=float, default=8.0, help="Delay between Food.com page requests")
    parser.add_argument("--cdn-delay", type=float, default=2.0, help="Delay between CDN image requests")
    parser.add_argument("--index-delay", type=float, default=12.0, help="Delay between Food.com sitemap index requests")
    parser.add_argument("--jitter", type=float, default=2.0, help="Random extra delay seconds")
    parser.add_argument("--concurrency", type=int, default=1, help="Concurrent recovery workers")
    parser.add_argument("--timeout", type=int, default=25, help="HTTP timeout seconds")
    parser.add_argument("--retries", type=int, default=2, help="Retries per request")
    parser.add_argument("--cooldown", type=int, default=1800, help="Cooldown seconds after HTTP 429")
    parser.add_argument("--min-bytes", type=int, default=5120, help="Minimum valid image size")
    parser.add_argument("--min-match", type=float, default=0.45, help="Minimum page title/name match score")
    args = parser.parse_args()

    logs_dir = Path(args.logs)
    logs_dir.mkdir(parents=True, exist_ok=True)
    pid_lock = acquire_pid_lock(logs_dir / "recovery_scraper.pid")
    try:
        RecoveryScraper(args).run()
    finally:
        pid_lock.__exit__(None, None, None)
        try:
            (logs_dir / "recovery_scraper.pid").unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()
