import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import feedparser
import requests
from bs4 import BeautifulSoup

JST = timezone(timedelta(hours=9))
DEADLINE_PATTERN = re.compile(r"(\d{1,2})[月/](\d{1,2})日?(?:\([^)]*\))?\s*(\d{1,2}):(\d{2})")
DEADLINE_FIELDS = ["抽選終了日時", "終了日", "受付終了日時"]

NYUKA_NOW_URL = os.environ.get("NYUKA_NOW_URL", "https://nyuka-now.com/archives/2459")
POKECAWATCH_FEED_URL = os.environ.get(
    "POKECAWATCH_FEED_URL",
    "https://pokecawatch.com/category/%E6%8A%BD%E9%81%B8%E3%83%BB%E4%BA%88%E7%B4%84%E6%83%85%E5%A0%B1/feed/",
)
GAMEPEDIA_URL = os.environ.get("GAMEPEDIA_URL", "https://premium.gamepedia.jp/pokeca/archives/124")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PokekaNotifier/1.0)"}

TARGET_SECTION_HEADINGS = {"抽選・予約応募受付中のストア", "近日受付開始予定のストア"}
TABLE_FIELD_ORDER = ["対象商品", "抽選形式", "開始日", "終了日", "当選発表", "応募条件", "応募ページ", "詳細ページ"]
GAMEPEDIA_FIELD_ORDER = [
    "対象商品",
    "販売種別",
    "抽選開始日時",
    "抽選終了日時",
    "抽選結果発表",
    "購入期間",
    "お届け日",
    "購入制限等",
    "種別",
    "受付終了日時",
]


def fetch_nyuka_now_entries():
    resp = requests.get(NYUKA_NOW_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html5lib")

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
                label = th.get_text(strip=True)
                row_data[label] = td.get_text(" ", strip=True)
                link = td.find("a", href=True)
                if link is not None:
                    row_data[f"{label}_url"] = link["href"]

            product = row_data.get("対象商品", "")
            key = f"{current_section}::{current_store}::{product}"
            entries[key] = {"section": current_section, "store": current_store, "data": row_data}

    return entries


def fetch_gamepedia_entries():
    resp = requests.get(GAMEPEDIA_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html5lib")

    accepting_heading = None
    for h3 in soup.find_all("h3"):
        span = h3.find("span")
        if span and span.get_text(strip=True) == "受付中のショップ":
            accepting_heading = h3
            break
    if accepting_heading is None:
        return {}

    rows = []
    current_product = None
    for tag in accepting_heading.find_all_next():
        if tag.name == "h2":
            break
        if tag.name == "h4":
            current_product = tag.get_text(strip=True)
        elif tag.name == "table" and current_product is not None:
            table_rows = tag.find_all("tr")
            col_labels = [th.get_text(strip=True) for th in table_rows[0].find_all("th")] if table_rows else []
            for row in table_rows[1:]:
                cells = row.find_all("td")
                if not cells:
                    continue
                a = cells[0].find("a", href=True)
                schedule_id = a["href"].lstrip("#") if a and a["href"].startswith("#product_schedule-") else None
                fallback_data = {label: cell.get_text(" ", strip=True) for label, cell in zip(col_labels, cells)}
                rows.append((current_product, schedule_id, fallback_data))

    entries = {}
    for product, schedule_id, fallback_data in rows:
        anchor = soup.find(id=schedule_id) if schedule_id else None
        table = anchor.find_next_sibling("table") if anchor is not None else None

        if table is not None:
            shop_name = anchor.get_text(strip=True)
            row_data = {}
            for row in table.find_all("tr"):
                th = row.find("th")
                td = row.find("td")
                if th is None or td is None:
                    continue
                row_data[th.get_text(strip=True)] = td.get_text(" ", strip=True)

            detail_url = None
            detail_div = table.find_next_sibling("div")
            if detail_div is not None:
                link = detail_div.find("a", href=True)
                if link is not None:
                    detail_url = link["href"]
        else:
            shop_name = fallback_data.get("ショップ", "不明ショップ")
            row_data = {"対象商品": product, **fallback_data}
            detail_url = None

        key = f"{product}::{schedule_id or shop_name}"
        entries[key] = {"product": product, "shop": shop_name, "data": row_data, "detail_url": detail_url}

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
    return {"nyuka_now": {}, "pokecawatch": {}, "gamepedia": {}, "reminders_sent": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def hash_entry(entry):
    return hashlib.sha256(json.dumps(entry, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def diff_hashed_entries(old, new):
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


def build_gamepedia_embed(entry, is_update):
    data = entry["data"]
    title_prefix = "🔄 抽選情報更新" if is_update else "🆕 新着抽選情報"
    fields = [
        {"name": label, "value": data[label][:1000], "inline": False}
        for label in GAMEPEDIA_FIELD_ORDER
        if label in data
    ]
    embed = {
        "title": f"{title_prefix}：{entry['shop']}",
        "description": "攻略大百科",
        "fields": fields,
        "color": 0xFFA500 if is_update else 0x9B59B6,
    }
    if entry.get("detail_url"):
        embed["url"] = entry["detail_url"]
    return embed


def parse_deadline(text, now):
    if not text:
        return None
    m = DEADLINE_PATTERN.search(text)
    if not m:
        return None
    month, day, hour, minute = (int(x) for x in m.groups())
    try:
        candidate = datetime(now.year, month, day, hour, minute, tzinfo=JST)
    except ValueError:
        return None
    if candidate < now - timedelta(days=1):
        candidate = candidate.replace(year=candidate.year + 1)
    return candidate


def extract_deadline(data, now):
    for field in DEADLINE_FIELDS:
        if field in data:
            deadline = parse_deadline(data[field], now)
            if deadline is not None:
                return deadline
    return None


def entry_display_info(source, entry):
    if source == "nyuka_now":
        data = entry["data"]
        name = f"{entry['store']}：{data.get('対象商品', '')}"
        url = data.get("応募ページ_url") or data.get("詳細ページ_url")
        label = "入荷Now"
    else:
        name = f"{entry['shop']}：{entry['product']}"
        url = entry.get("detail_url")
        label = "攻略大百科"
    return name, label, url


def build_reminder_embed(name, label, url, tag):
    embed = {
        "title": f"⏰ {tag}：{name}",
        "description": f"{label} - 応募をお忘れなく",
        "color": 0xE74C3C,
    }
    if url:
        embed["url"] = url
    return embed


def collect_deadline_reminders(nyuka_entries, gamepedia_entries, now, reminded):
    today = now.date()
    tomorrow = today + timedelta(days=1)
    updated_reminded = dict(reminded)
    embeds = []

    for source, entries in (("nyuka_now", nyuka_entries), ("gamepedia", gamepedia_entries)):
        for key, entry in entries.items():
            deadline = extract_deadline(entry["data"], now)
            if deadline is None:
                continue
            deadline_date = deadline.date()
            if deadline_date == today:
                tag = "本日締切"
            elif deadline_date == tomorrow:
                tag = "明日締切"
            else:
                continue

            reminder_key = f"{source}::{key}"
            sent_tags = updated_reminded.get(reminder_key, [])
            if tag in sent_tags:
                continue

            name, label, url = entry_display_info(source, entry)
            embeds.append(build_reminder_embed(name, label, url, tag))
            updated_reminded[reminder_key] = sent_tags + [tag]

    valid_keys = {f"nyuka_now::{k}" for k in nyuka_entries} | {f"gamepedia::{k}" for k in gamepedia_entries}
    cleaned_reminded = {k: v for k, v in updated_reminded.items() if k in valid_keys}
    return embeds, cleaned_reminded


def send_discord_embeds(embeds):
    if not embeds:
        return True
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL is not set; skipping notification", file=sys.stderr)
        return False
    ok = True
    for i in range(0, len(embeds), 10):
        chunk = embeds[i : i + 10]
        resp = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": chunk}, timeout=30)
        if resp.status_code >= 300:
            print(f"Discord webhook failed: {resp.status_code} {resp.text}", file=sys.stderr)
            ok = False
    return ok


def main():
    state = load_state()
    is_first_run = not any(state.get(key) for key in ("nyuka_now", "pokecawatch", "gamepedia"))
    is_gamepedia_first_run = not state.get("gamepedia")

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

    try:
        gamepedia_entries = fetch_gamepedia_entries()
    except Exception as e:
        print(f"攻略大百科取得エラー: {e}", file=sys.stderr)
        gamepedia_entries = state.get("gamepedia", {})

    new_items, updated_items = diff_hashed_entries(state.get("nyuka_now", {}), nyuka_entries)
    new_articles = diff_pokecawatch(state.get("pokecawatch", {}), pokecawatch_entries)
    new_gamepedia, updated_gamepedia = diff_hashed_entries(state.get("gamepedia", {}), gamepedia_entries)

    now = datetime.now(JST)
    reminder_embeds, reminders_sent = collect_deadline_reminders(
        nyuka_entries, gamepedia_entries, now, state.get("reminders_sent", {})
    )

    if is_first_run:
        print("初回実行のため、新着・更新の通知は送らずに状態のみ保存します。")
        embeds = reminder_embeds
    else:
        gamepedia_embeds = []
        if is_gamepedia_first_run:
            print("攻略大百科は今回追加されたため、初回分の通知はスキップします。")
        else:
            gamepedia_embeds = [build_gamepedia_embed(e, is_update=False) for e in new_gamepedia] + [
                build_gamepedia_embed(e, is_update=True) for e in updated_gamepedia
            ]

        embeds = (
            [build_nyuka_embed(e, is_update=False) for e in new_items]
            + [build_nyuka_embed(e, is_update=True) for e in updated_items]
            + gamepedia_embeds
            + [build_pokecawatch_embed(e) for e in new_articles]
            + reminder_embeds
        )

    sent_ok = send_discord_embeds(embeds)
    if sent_ok:
        print(f"{len(embeds)}件の通知を送信しました。")
        save_state(
            {
                "nyuka_now": nyuka_entries,
                "pokecawatch": pokecawatch_entries,
                "gamepedia": gamepedia_entries,
                "reminders_sent": reminders_sent,
            }
        )
    else:
        print(f"{len(embeds)}件の通知の送信に失敗したため、状態を更新せず次回すべて再試行します。", file=sys.stderr)


if __name__ == "__main__":
    main()
