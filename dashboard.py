"""抽選情報を「見やすい一覧ページ（dashboard.html）」にまとめるスクリプト。

使い方:  python dashboard.py
→ 同じフォルダに dashboard.html ができて、ブラウザで自動的に開きます。
"""
import json
import os
import re
import sys
import time
import unicodedata
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

from main import JST, fetch_gamepedia_entries, fetch_nyuka_now_entries, fetch_pokecawatch_entries

# DASHBOARD_OUT で書き出し先を変えられる（GitHub Actions 用）
OUTPUT_FILE = Path(os.environ.get("DASHBOARD_OUT") or Path(__file__).with_name("dashboard.html"))
TEMPLATE_FILE = Path(__file__).with_name("dashboard_template.html")

# 入荷Nowの「抽選・予約情報まとめ」ページ（トレカの種類, ページURL）
NYUKA_TCG_PAGES = [
    ("ポケカ", "https://nyuka-now.com/archives/2459"),
    ("ワンピース", "https://nyuka-now.com/archives/97393"),
    ("遊戯王", "https://nyuka-now.com/archives/72605"),
    ("遊戯王", "https://nyuka-now.com/archives/100529"),  # ラッシュデュエル
    ("デュエマ", "https://nyuka-now.com/archives/140866"),
    ("ドラゴンボール", "https://nyuka-now.com/archives/141863"),
    ("ユニオンアリーナ", "https://nyuka-now.com/archives/134874"),
    ("ヴァイス", "https://nyuka-now.com/archives/130997"),
    ("デジモン", "https://nyuka-now.com/archives/141440"),
    ("ガンダム", "https://nyuka-now.com/archives/145923"),
    ("hololive", "https://nyuka-now.com/archives/144497"),
    ("シャドバ", "https://nyuka-now.com/archives/89325"),
    ("ロルカナ", "https://nyuka-now.com/archives/146927"),
    ("MTG", "https://nyuka-now.com/archives/152554"),
    ("ウルトラマン", "https://nyuka-now.com/archives/144495"),
    ("コナン", "https://nyuka-now.com/archives/143006"),
]

# 「2026年9月28日(日)12:00」「9/28 12:00」「9月28日」などを読み取る
DATE_PATTERN = re.compile(
    r"(?:(\d{4})\s*年\s*)?(\d{1,2})\s*[月/]\s*(\d{1,2})\s*日?"
    r"(?:\s*[(（][^)）]*[)）])?"
    r"(?:\s*(\d{1,2})\s*[:：時]\s*(\d{1,2})?)?"
)


# ---------- 日付の読み取り ----------

def parse_date(text, now, is_end):
    """文字列から日時を取り出す。読めなければ None。"""
    if not text:
        return None
    m = DATE_PATTERN.search(unicodedata.normalize("NFKC", text))
    if not m:
        return None
    year, month, day, hour, minute = m.groups()
    if hour is None:
        hour, minute = (23, 59) if is_end else (0, 0)
    try:
        hour, minute = int(hour), int(minute or 0)
        if hour == 24:
            hour, minute = 23, 59
        if year:
            return datetime(int(year), int(month), int(day), hour, minute, tzinfo=JST)
        candidates = [
            datetime(y, int(month), int(day), hour, minute, tzinfo=JST)
            for y in (now.year - 1, now.year, now.year + 1)
        ]
    except ValueError:
        return None
    if not is_end:
        # 開始日は「1か月より先」はまず無いので、それより前で一番新しい年を選ぶ
        return max(d for d in candidates if d <= now + timedelta(days=30))
    # 終了日は今日に一番近い年を選ぶ
    return min(candidates, key=lambda d: abs(d - now))


def first_date(data, fields, now, is_end):
    for field in fields:
        d = parse_date(data.get(field), now, is_end)
        if d is not None:
            return d
    return None


# ---------- 各サイトの情報を同じ形にそろえる ----------

def normalize_nyuka(entry, now):
    data = entry["data"]
    return {
        "product": data.get("対象商品", ""),
        "shop": entry["store"],
        "source": "入荷Now",
        "method": data.get("抽選形式") or data.get("販売形式") or data.get("配布形式", ""),
        "start": first_date(data, ["開始日"], now, is_end=False),
        "end": first_date(data, ["終了日"], now, is_end=True),
        "announce": data.get("当選発表", ""),
        "condition": data.get("応募条件", ""),
        "url": data.get("応募ページ_url") or data.get("販売ページ_url") or data.get("詳細ページ_url") or data.get("対象商品_url"),
        "upcoming_section": entry["section"] == "近日受付開始予定のストア",
    }


def normalize_gamepedia(entry, now):
    data = entry["data"]
    return {
        "product": entry["product"],
        "shop": entry["shop"],
        "source": "攻略大百科",
        "method": data.get("販売種別") or data.get("種別", ""),
        "start": first_date(data, ["抽選開始日時"], now, is_end=False),
        "end": first_date(data, ["抽選終了日時", "受付終了日時"], now, is_end=True),
        "announce": data.get("抽選結果発表", ""),
        "condition": data.get("購入制限等", ""),
        "url": entry.get("detail_url"),
        "upcoming_section": False,
    }


def dedupe_key(item):
    text = unicodedata.normalize("NFKC", f"{item['shop']}|{item['product']}")
    text = text.replace("ポケモンカード", "").replace("ゲーム", "")
    return re.sub(r"[\s・:：、,（）()]", "", text).lower()


def classify(item, now):
    """状態を決める。終わったものは None（＝表示しない）。"""
    start, end = item["start"], item["end"]
    if end is not None and end < now:
        return None
    if (start is not None and start > now) or (start is None and item["upcoming_section"]):
        return "soon"
    if end is None:
        return "unknown"
    if end.date() == now.date():
        return "today"
    if end.date() == now.date() + timedelta(days=1):
        return "tomorrow"
    return "open"


def build_items(now):
    raw, errors = [], []
    jobs = [(game, lambda url=url: fetch_nyuka_now_entries(url), normalize_nyuka) for game, url in NYUKA_TCG_PAGES]
    jobs.append(("ポケカ", fetch_gamepedia_entries, normalize_gamepedia))
    for game, fetch, normalize in jobs:
        try:
            raw += [dict(normalize(e, now), game=game) for e in fetch().values()]
        except Exception as e:  # 1つのページが落ちても他は表示する
            errors.append(f"{game} の情報の一部が取れませんでした（{e.__class__.__name__}）")
            print(f"{game}取得エラー: {e}", file=sys.stderr)
        time.sleep(1)  # 相手のサイトに負担をかけないよう1秒あける

    # 重複をまとめる（情報が多い方を残す）
    merged, games = {}, {}
    for item in raw:
        key = dedupe_key(item)
        games.setdefault(key, [])
        if item["game"] not in games[key]:
            games[key].append(item["game"])
        score = sum(1 for v in item.values() if v)
        if key not in merged or score > merged[key][0]:
            merged[key] = (score, item)

    items = []
    for _, item in merged.values():
        status = classify(item, now)
        if status is None:
            continue
        key = dedupe_key(item)
        item = dict(item, status=status, id=key, games=games[key])
        del item["game"]
        item["start"] = item["start"].isoformat() if item["start"] else None
        item["end"] = item["end"].isoformat() if item["end"] else None
        del item["upcoming_section"]
        items.append(item)

    items.sort(key=lambda i: (i["end"] is None, i["end"] or "", i["shop"]))
    return items, errors


def build_news():
    try:
        return [{"title": e["title"], "url": e["link"]} for e in fetch_pokecawatch_entries().values()][:10]
    except Exception as e:
        print(f"ポケカウォッチ取得エラー: {e}", file=sys.stderr)
        return []


def main():
    now = datetime.now(JST)
    items, errors = build_items(now)
    payload = {
        "generatedAt": now.isoformat(),
        "items": items,
        "news": build_news(),
        "errors": errors,
    }
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE_FILE.read_text(encoding="utf-8").replace("/*__DATA__*/null", data_json)
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(html, encoding="utf-8")
    print(f"{len(items)}件の抽選情報を {OUTPUT_FILE.name} に書き出しました。")
    if "--no-open" not in sys.argv:
        webbrowser.open(OUTPUT_FILE.as_uri())


if __name__ == "__main__":
    main()
