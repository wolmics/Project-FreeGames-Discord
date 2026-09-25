from __future__ import annotations

import hashlib
import json
import logging
import sys
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional
from io import BytesIO

from PIL import Image

import requests
import yaml

# --- Paths & constants ---

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
STATE_PATH = BASE_DIR / "sent_games.json"
COLOR_CACHE_PATH = BASE_DIR / "color_cache.json"
LOG_PATH = BASE_DIR / "logs.log"

DEFAULT_API_URL = ""
DEFAULT_REQUEST_TIMEOUT = 15          # seconds
DEFAULT_MISSING_RUN_THRESHOLD = 2     # absences of an offer before it is dropped
STATE_RETENTION_DAYS = 400            # hard backstop so the state file cant grow forever
COLOR_CACHE_RETENTION_DAYS = 400      # no point in caching a color forever

FALLBACK_COLOR = 0x2B2D31             # used if we cant fetch/analyze the image (default discord dark gray)
IMAGE_FETCH_TIMEOUT = 5               # seconds

# "large"  -> Components V2 container (build_components)
# "compact" -> classic embed payload (build_embed)
DEFAULT_MESSAGE_STYLE = "large"
VALID_MESSAGE_STYLES = {"large", "compact"}

DESCRIPTION_LIMITS = {
    "compact": 150,
    "large": 220,
}

SHOP_ICONS = {
    "epicgames": "https://wolmics.cc/cdn/epicgames.png",
    "steam": "https://wolmics.cc/cdn/steam.png",
    "gog": "https://wolmics.cc/cdn/gog.png",
}

REDIRECT_BASE_URL = "https://wolmics.cc/open"
BOT_USERNAME = "Project Free Games"
BOT_AVATAR_URL = "https://wolmics.cc/cdn/icon.png"

# --- Logging ---

def setup_logging() -> logging.Logger:
    logger = logging.getLogger("free_games_bot")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    fmt = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", "%H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    # 5 MB per file, 5 backups kept -> bounded disk usage over months/years,
    # unlike a single ever-growing logs.log.
    file_handler = RotatingFileHandler(LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger

log = setup_logging()

# --- Config ---

@dataclass
class Webhook:
    name: str
    url: str


@dataclass
class Config:
    webhooks: list[Webhook]
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT
    missing_run_threshold: int = DEFAULT_MISSING_RUN_THRESHOLD
    api_url: str = DEFAULT_API_URL
    message_style: str = DEFAULT_MESSAGE_STYLE


def load_config(path: Path) -> Config:
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found at {path}. Copy config.example.yaml to "
            f"config.yaml and fill in your webhook(s)."
        )

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    webhook_entries = raw.get("webhooks") or []
    if not webhook_entries:
        raise ValueError("config.yaml must define at least one entry under 'webhooks:'.")

    webhooks: list[Webhook] = []
    seen_names = set()
    for entry in webhook_entries:
        name = str(entry.get("name", "")).strip()
        url = str(entry.get("url", "")).strip()
        if not name or not url:
            raise ValueError(f"Every webhook needs a 'name' and a 'url', got: {entry}")
        if name in seen_names:
            raise ValueError(f"Duplicate webhook name '{name}' in config.yaml - names must be unique.")
        seen_names.add(name)
        webhooks.append(Webhook(name=name, url=url))

    settings = raw.get("settings") or {}

    message_style = str(settings.get("style", DEFAULT_MESSAGE_STYLE)).strip().lower()
    if message_style not in VALID_MESSAGE_STYLES:
        raise ValueError(
            f"settings.style must be one of {sorted(VALID_MESSAGE_STYLES)}, got '{message_style}'."
        )

    return Config(
        webhooks=webhooks,
        request_timeout=int(settings.get("request_timeout", DEFAULT_REQUEST_TIMEOUT)),
        missing_run_threshold=int(settings.get("missing_run_threshold", DEFAULT_MISSING_RUN_THRESHOLD)),
        api_url=str(settings.get("api_url", DEFAULT_API_URL)),
        message_style=message_style,
    )


def load_state(path: Path) -> dict:
    """ State: per webhooks which games we have already sent"""
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        log.exception("sent_games.json is unreadable/corrupt - starting from empty state.")
        try:
            path.rename(path.with_suffix(".json.corrupt"))
        except Exception:
            pass
        return {}


def save_state(path: Path, state: dict) -> None:
    tmp_path = path.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    tmp_path.replace(path)


def make_offer_key(name: str, shop: str) -> str:
    """Identifies a (game, shop) offer"""
    basis = f"{name.strip().lower()}|{shop.strip().lower()}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def prune_old_entries(webhook_state: dict) -> int:
    cutoff = datetime.now(timezone.utc).timestamp() - STATE_RETENTION_DAYS * 86400
    stale_keys = []
    for key, record in webhook_state.items():
        first_sent = record.get("first_sent_at")
        try:
            sent_ts = datetime.fromisoformat(first_sent).timestamp() if first_sent else 0
        except ValueError:
            sent_ts = 0
        if sent_ts and sent_ts < cutoff:
            stale_keys.append(key)
    for key in stale_keys:
        del webhook_state[key]
    return len(stale_keys)



# --- Colour cache ---

def load_color_cache(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        log.exception("color_cache.json is unreadable/corrupt - starting from empty cache.")
        try:
            path.rename(path.with_suffix(".json.corrupt"))
        except Exception:
            pass
        return {}


def save_color_cache(path: Path, cache: dict) -> None:
    tmp_path = path.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, sort_keys=True)
    tmp_path.replace(path)


def make_image_key(image_url: str) -> str:
    return hashlib.sha256(image_url.encode("utf-8")).hexdigest()[:16]

def prune_color_cache(cache: dict) -> int:
    cutoff = datetime.now(timezone.utc).timestamp() - COLOR_CACHE_RETENTION_DAYS * 86400
    stale_keys = []
    for key, record in cache.items():
        cached_at = record.get("cached_at")
        try:
            cached_ts = datetime.fromisoformat(cached_at).timestamp() if cached_at else 0
        except ValueError:
            cached_ts = 0
        if cached_ts and cached_ts < cutoff:
            stale_keys.append(key)
    for key in stale_keys:
        del cache[key]
    return len(stale_keys)


# --- Free games API ---

def fetch_free_games(api_url: str, timeout: int) -> tuple[list[dict], bool]:
    """
    Returns (games, success). success=False means the fetch failed or the
    response was malformed - callers must NOT treat that as "nothing is
    free right now", or state gets wiped and everything gets re-announced.
    """
    try:
        response = requests.get(api_url, timeout=timeout)
        response.raise_for_status()
        data = response.json()
    except Exception:
        log.exception("Failed to fetch free games from %s", api_url)
        return [], False

    if not isinstance(data, list):
        log.error("Unexpected API response shape (expected a list, got %s)", type(data).__name__)
        return [], False

    return data, True


def parse_game(raw: dict) -> Optional[dict]:
    """Defensively extract fields. Returns None (and logs) if unusable,
    so one bad entry can't take down the whole run."""
    name = raw.get("name")
    if not name or not isinstance(name, str):
        log.error("Skipping game entry with no usable name: %s", raw)
        return None

    link = raw.get("link")
    if not link:
        log.error("Skipping game entry with no usable link: %s", raw)
        return None

    shop_raw = raw.get("shop")
    shop = shop_raw if isinstance(shop_raw, str) and shop_raw else "unknown"

    expiration_raw = raw.get("expiration")
    try:
        datetime.fromisoformat(expiration_raw)
    except (TypeError, ValueError):
        log.error("Skipping '%s': unparsable expiration %r", name, expiration_raw)
        return None

    return {
        "name": name,
        "shop": shop,
        "expiration": expiration_raw,
        "description": raw.get("description", ""),
        "normal_price": raw.get("normal_price"),
        "image": raw.get("image"),
        "link": link,
    }


# --- Discord helpers ---

def truncate_description(text: str, style: str = "compact", limit: int = 200) -> str:
    """Cap a description at 'limit' chars"""
    limit = DESCRIPTION_LIMITS.get(style, limit)
    text = text.strip()
    if len(text) <= limit:
        return text

    cut = text[:limit].rsplit(" ", 1)[0]
    return f"{cut}…"


def to_discord_countdown(iso_timestamp: str) -> str:
    """Turn an ISO-8601 timestamp into Discords native relative-time"""
    dt = datetime.fromisoformat(iso_timestamp)
    unix_ts = int(dt.timestamp())
    return f"<t:{unix_ts}:R>"

def open_app_link(url: str) -> str:
    """Converts a Steam or Epic Games store link into a open-link"""
    steam_match = re.search(r"store\.steampowered\.com/app/(\d+)", url)
    if steam_match:
        return f"{REDIRECT_BASE_URL}/steam/{steam_match.group(1)}"

    epic_match = re.search(r"store\.epicgames\.com/.+?/p/([\w-]+)", url)
    if epic_match:
        return f"{REDIRECT_BASE_URL}/epic/{epic_match.group(1)}"

    return url

def dominant_color(image_url: str, timeout: int = IMAGE_FETCH_TIMEOUT) -> int:
    """
    Download the games image and pick out its dominant color,
    so the embeds side bar matches the artwork.
    Falls back to FALLBACK_COLOR on any failure.
    """
    try:
        resp = requests.get(image_url, timeout=timeout)
        resp.raise_for_status()

        img = Image.open(BytesIO(resp.content)).convert("RGB")
        img = img.resize((100, 100))  # downscale

        # Quantize down to a handful of clusters and take the largest one
        paletted = img.quantize(colors=5, method=Image.Quantize.FASTOCTREE)
        palette = paletted.getpalette()
        _, dominant_index = max(paletted.getcolors(), key=lambda c: c[0])
        r, g, b = palette[dominant_index * 3 : dominant_index * 3 + 3]

        return (r << 16) + (g << 8) + b
    except Exception as e:
        log.warning("Couldn't extract a color from '%s', using fallback, %s", image_url, e)
        return FALLBACK_COLOR


def dominant_color_cached(image_url: str, cache: dict, timeout: int = IMAGE_FETCH_TIMEOUT) -> int:
    """Look up a previously computed color for this image before making a request
    Colors are cached across runs and reused across every webhook"""
    key = make_image_key(image_url)
    cached = cache.get(key)
    if cached is not None:
        return cached["color"]

    color = dominant_color(image_url, timeout)
    cache[key] = {"color": color, "cached_at": now_iso()}
    return color


def shop_icon(shop: str) -> str:
    return SHOP_ICONS.get(shop, "")

# --- Payload builders ---

def build_components(game: dict, color_cache: dict) -> dict:
    """Builds a Components V2 Container ("large" style)."""

    price_line = (
        f"~~{game['normal_price']}~~ -> FREE"
        if game.get("normal_price")
        else "**FREE**"
    )

    # Title
    text_blocks = [
        {"type": 10, "content": f"### [{game['name']}]({game['link']}) [↗]({open_app_link(game['link'])})"},
    ]

    # Description
    description = truncate_description(game.get("description", ""), "large")
    if description:
        text_blocks.append({"type": 10, "content": description})

    # Icon
    icon = shop_icon(game["shop"])
    container_components = []

    if icon:
        container_components.append(
            {
                "type": 9,
                "components": text_blocks,
                "accessory": {"type": 11, "media": {"url": icon}},
            }
        )
    else:
        container_components.extend(text_blocks)

    # Price & Expiration
    expiration_spacing = max(0, 10 - len(game['normal_price']))

    container_components.append({"type": 14, "divider": False, "spacing": 1})
    container_components.append(
        {
            "type": 10,
            "content": (
                f"**Price**{' ' * 21}**Free until**\n"
                f"{price_line}{' ' * expiration_spacing}{to_discord_countdown(game['expiration'])}"
            ),
        }
    )
    container_components.append({"type": 14, "divider": False, "spacing": 1})

    # Image
    if game.get("image"):
        container_components.append(
            {
                "type": 12,
                "items": [{"media": {"url": game["image"]}}],
            }
        )
        accent_color = dominant_color_cached(game["image"], color_cache)
    else:
        accent_color = FALLBACK_COLOR

    container_components.append({"type": 14, "divider": True, "spacing": 1})

    # Footer
    container_components.append(
        {
            "type": 10,
            "content": "-# Project Free Games • visit at [wolmics.cc](https://wolmics.cc) • [source code](https://github.com/wolmics/Project-FreeGames-Discord)",
        }
    )

    container = {
        "type": 17,
        "accent_color": accent_color,
        "components": container_components,
    }

    return container 


def build_embed(game: dict, color_cache: dict) -> dict:
    """Builds a classic embed (compact style)"""

    price_line = (
        f"~~{game['normal_price']}~~ -> **FREE**"
        if game.get("normal_price")
        else "**FREE**"
    )

    embed = {
        "title": game["name"],
        "url": game["link"],
        "description": truncate_description(game.get("description", ""), "compact"),
        "fields": [
            {
                "name": "Price",
                "value": price_line,
                "inline": True,
            },
            {
                "name": "Free until",
                "value": to_discord_countdown(game["expiration"]),
                "inline": True,
            },
        ],
    }

    icon = shop_icon(game["shop"])
    if icon:
        embed["thumbnail"] = {"url": icon}

    if game.get("image"):
        embed["image"] = {"url": game["image"]}
        embed["color"] = dominant_color_cached(game["image"], color_cache)
    else:
        embed["color"] = FALLBACK_COLOR

    return embed


def build_payload(game: dict, color_cache: dict, style: str) -> dict:
    """Uses the Components V2 container ("large") or the classic embed payload ("compact")"""
    payload = {
        "username": BOT_USERNAME,
        "avatar_url": BOT_AVATAR_URL,
    }

    if style == "compact":
        payload["embeds"] = [build_embed(game, color_cache)]
    else:
        payload["flags"] = 1 << 15  # IS_COMPONENTS_V2
        payload["components"] = [build_components(game, color_cache)]

    return payload


def send_discord(webhook: "Webhook", game: dict, timeout: int, style: str) -> bool:
    """ game["_components"] is built once per game in run(), then reused for every webhook"""
    payload = game["_components"]

    # with_components=true is only send for Components V2 payloads
    url = f"{webhook.url}?with_components=true" if style == "large" else webhook.url

    try:
        response = requests.post(
            url,
            json=payload,
            timeout=timeout,
        )
    except Exception:
        log.exception(
            "Network error sending '%s' to webhook '%s'", game["name"], webhook.name
        )
        return False

    if response.status_code == 204:
        log.info("Sent '%s' to webhook '%s'.", game["name"], webhook.name)
        return True

    log.error(
        "Discord rejected '%s' for webhook '%s': status=%s body=%s",
        game["name"], webhook.name, response.status_code, response.text,
    )
    return False


# --- Core run ---

def process_webhook(webhook: Webhook, games: list[dict], webhook_state: dict, cfg: Config) -> bool:
    """Processes one Webhook. Sends any new offers and removes old ones"""
    current_keys = set()
    changed = False

    # Send anything new
    for game in games:
        key = make_offer_key(game["name"], game["shop"])
        current_keys.add(key)

        if key in webhook_state:
            record = webhook_state[key]
            if record.get("missing_runs", 0) != 0:
                record["missing_runs"] = 0
                changed = True
            continue

        if send_discord(webhook, game, cfg.request_timeout, cfg.message_style):
            webhook_state[key] = {
                "name": game["name"],
                "shop": game["shop"],
                "expiration": game["expiration"],
                "first_sent_at": now_iso(),
                "missing_runs": 0,
            }
            changed = True
        # If the send failed, we dont record it, so its
        # retried next run instead of being silently lost.
    
    # Removes anything that is no longer in free
    for key in list(webhook_state.keys()):
        if key in current_keys:
            continue
        record = webhook_state[key]
        record["missing_runs"] = record.get("missing_runs", 0) + 1
        changed = True
        if record["missing_runs"] >= cfg.missing_run_threshold:
            log.info(
                "'%s' on '%s' is no longer listed as free - removing from tracking.",
                record.get("name", "?"), webhook.name,
            )
            del webhook_state[key]

    if prune_old_entries(webhook_state):
        changed = True

    return changed


def run() -> None:
    # Config
    try:
        cfg = load_config(CONFIG_PATH)
    except Exception:
        log.exception("Could not load config.yaml - aborting.")
        return

    state = load_state(STATE_PATH)
    color_cache = load_color_cache(COLOR_CACHE_PATH)

    # Load Games
    raw_games, ok = fetch_free_games(cfg.api_url, cfg.request_timeout)
    if not ok:
        log.error("Skipping this run: API fetch failed. State left untouched.")
        return

    games = [g for g in (parse_game(raw) for raw in raw_games) if g is not None]
    if not games:
        log.info("API returned no usable games this run. State left untouched.")
        return
    
    # Builds Payload for each game
    cache_keys_before = set(color_cache.keys())
    for game in games:
        game["_components"] = build_payload(game, color_cache, cfg.message_style)

    # Builds / gets Color for each game
    pruned = prune_color_cache(color_cache)
    if pruned or set(color_cache.keys()) != cache_keys_before:
        try:
            save_color_cache(COLOR_CACHE_PATH, color_cache)
        except Exception:
            log.exception("Failed to write color_cache.json - colors will be recomputed next run.")

    # Sends Games to webhooks, if they haven been send
    state_changed = False
    for webhook in cfg.webhooks:
        webhook_state = state.setdefault(webhook.name, {})
        if process_webhook(webhook, games, webhook_state, cfg):
            state_changed = True

    # Save
    if state_changed:
        try:
            save_state(STATE_PATH, state)
        except Exception:
            log.exception("Failed to write sent_games.json - state for this run may be lost.")
            return

    log.info("Done. Processed %d game(s) for %d webhook(s).", len(games), len(cfg.webhooks))


if __name__ == "__main__":
    try:
        run()
    except Exception:
        log.exception("Unhandled error running the bot.")