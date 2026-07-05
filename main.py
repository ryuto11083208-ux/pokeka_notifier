import hashlib
import json
import os
import sys

import feedparser
import requests
from bs4 import BeautifulSoup

NYUKA_NOW_URL = os.environ.get("NYUKA_NOW_URL", "https://nyuka-now.com/archives/2459")
POKECAWATCH_FEED_URL = os.environ.get(
    "POKECAWATCH_FEED_URL",
    "https://pokecawatch.com/category/%E6%8A%BD%E9%81%B8%E3%83%BB%E4%BA%88%E7%B4%84%E6%83%85%E5%A0%B1/feed/",
)
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PokekaNotifier/1.0)"}

TARGET_SECTION_HEADINGS = {"抽選・予約応募受付中のストア", "近日受付開始予定のストア"}
TABLE_FIELD_ORDER = ["対象商品", "抽選形式", "開始日", "終了日", "当選発表", "応募条件", "応募ページ", "詳細ページ"]


def fetch_nyuka_now_entries():
    resp = requests.get(NYUKA_NOW_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")

    entries = {}
    current_section = None
    current_store = None

    for tag in soup.find_all(["h2", "h3", "figure"]):
        if tag.name == "h2":
            heading = tag.get_text(strip=True)
            current_section = heading if heading in TARGET_SECTION_HEADINGS else None
            current_store = None
        elif tag.name == "h3":
            if current_section is not None:
                current_store = tag.get_text(strip=True)
        elif tag.name == "figure":
            if current_section is None or current_store is None:
                continue
            if "wp-block-table" not in (tag.get("class") or []):
                continue

            row_data = {}
            for row in tag.find_all("tr"):
                th = row.find("th")
                td = row.find("td")
                if th is None or td is None:
                    continue
                row_data[th.get_text(strip=True)] = td.get_text(" ", strip=True)

            product = row_data.get("対象商品", "")
            key = f"{current_section}::{current_store}::{product}"
            entries[key] = {"section": current_section, "store": current_store, "data": row_data}

    return entries


def fetch_pokecawatch_entries():
    feed = feedparser.parse(POKECAWATCH_FEED_URL)
    entries = {}
    for item in feed.entries:
        key = item.get("id") or item.get("link")
        if not key:
            continue
        entries[key] = {
            "title": item.get("title", ""),
            "link": item.get("link", ""),
            "published": item.get("published", ""),
        }
    return entries


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"nyuka_now": {}, "pokecawatch": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def hash_entry(entry):
    return hashlib.sha256(json.dumps(entry, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def diff_nyuka_now(old, new):
    old_hashes = {key: hash_entry(entry) for key, entry in old.items()}
    new_items, updated_items = [], []
    for key, entry in new.items():
        if key not in old_hashes:
            new_items.append(entry)
        elif old_hashes[key] != hash_entry(entry):
            updated_items.append(entry)
    return new_items, updated_items


def diff_pokecawatch(old, new):
    return [entry for key, entry in new.items() if key not in old]


def build_nyuka_embed(entry, is_update):
    data = entry["data"]
    title_prefix = "🔄 抽選情報更新" if is_update else "🆕 新着抽選情報"
    fields = [
        {"name": label, "value": data[label][:1000], "inline": False}
        for label in TABLE_FIELD_ORDER
        if label in data
    ]
    return {
        "title": f"{title_prefix}：{entry['store']}",
        "description": entry["section"],
        "fields": fields,
        "color": 0xFFA500 if is_update else 0x00A86B,
    }


def build_pokecawatch_embed(entry):
    return {"title": f"📰 ポケカウォッチ新着：{entry['title']}", "url": entry["link"], "color": 0x3498DB}


def send_discord_embeds(embeds):
    if not embeds:
        return
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL is not set; skipping notification", file=sys.stderr)
        return
    for i in range(0, len(embeds), 10):
        chunk = embeds[i : i + 10]
        resp = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": chunk}, timeout=30)
        if resp.status_code >= 300:
            print(f"Discord webhook failed: {resp.status_code} {resp.text}", file=sys.stderr)


def main():
    state = load_state()
    is_first_run = not state.get("nyuka_now") and not state.get("pokecawatch")

    try:
        nyuka_entries = fetch_nyuka_now_entries()
    except Exception as e:
        print(f"入荷Now取得エラー: {e}", file=sys.stderr)
        nyuka_entries = state.get("nyuka_now", {})

    try:
        pokecawatch_entries = fetch_pokecawatch_entries()
    except Exception as e:
        print(f"ポケカウォッチ取得エラー: {e}", file=sys.stderr)
        pokecawatch_entries = state.get("pokecawatch", {})

    new_items, updated_items = diff_nyuka_now(state.get("nyuka_now", {}), nyuka_entries)
    new_articles = diff_pokecawatch(state.get("pokecawatch", {}), pokecawatch_entries)

    if is_first_run:
        print("初回実行のため、通知は送らずに状態のみ保存します。")
    else:
        embeds = (
            [build_nyuka_embed(e, is_update=False) for e in new_items]
            + [build_nyuka_embed(e, is_update=True) for e in updated_items]
            + [build_pokecawatch_embed(e) for e in new_articles]
        )
        send_discord_embeds(embeds)
        print(f"{len(embeds)}件の通知を送信しました。")

    save_state({"nyuka_now": nyuka_entries, "pokecawatch": pokecawatch_entries})


if __name__ == "__main__":
    main()
