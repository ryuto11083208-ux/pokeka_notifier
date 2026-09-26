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

import requests
from bs4 import BeautifulSoup

from pricing import attach_prices
from main import JST, fetch_gamepedia_entries, fetch_nyuka_now_entries, fetch_pokecawatch_entries

# DASHBOARD_OUT で書き出し先を変えられる（GitHub Actions 用）
OUTPUT_FILE = Path(os.environ.get("DASHBOARD_OUT") or Path(__file__).with_name("dashboard.html"))
TEMPLATE_FILE = Path(__file__).with_name("dashboard_template.html")

# 入荷Nowの「抽選・予約情報まとめ」ページ（カテゴリ, 種類, ページ番号）
NYUKA_PAGES = [
    ("トレカ", "ポケカ", 2459),
    ("トレカ", "ワンピース", 97393),
    ("トレカ", "遊戯王", 72605),
    ("トレカ", "遊戯王", 100529),  # ラッシュデュエル
    ("トレカ", "デュエマ", 140866),
    ("トレカ", "ドラゴンボール", 141863),
    ("トレカ", "ユニオンアリーナ", 134874),
    ("トレカ", "ヴァイス", 130997),
    ("トレカ", "デジモン", 141440),
    ("トレカ", "ガンダム", 145923),
    ("トレカ", "hololive", 144497),
    ("トレカ", "シャドバ", 89325),
    ("トレカ", "ロルカナ", 146927),
    ("トレカ", "MTG", 152554),
    ("トレカ", "ウルトラマン", 144495),
    ("トレカ", "コナン", 143006),
    ("トレカ", "カードダス", 137229),
    ("スニーカー", "ナイキ", 94961),
    ("ホビー", "ガンプラ", 17197),
    ("ホビー", "ガンプラ", 134954),  # 30MM
    ("ホビー", "ガンプラ", 142273),  # 解体匠機
    ("ホビー", "ガンプラ", 148800),  # アーセナルベース
    ("ホビー", "フィギュア", 100970),
    ("ホビー", "フィギュア", 125879),  # 30MS
    ("ホビー", "仮面ライダー", 75339),
    ("ホビー", "ソフビ", 94093),
    ("ホビー", "LABUBU", 153107),
    ("ホビー", "amiibo", 110080),
    ("ホビー", "たまごっち", 147287),
    ("ホビー", "シール", 156722),
    ("ホビー", "ウマ娘", 99419),
    ("ホビー", "ポケモン30周年", 157639),
]

SNEAKERWARS_TOP = "https://sneakerwars.jp/"
SNEAKERWARS_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; LotteryBoard/1.0)"}
# 品番（例: IQ5495-005 / KH6730 / U9835DU）
STYLE_CODE = re.compile(r"([A-Z]{1,3}\d{3,5}-\d{3}|[A-Z]{2}\d{4}|U\d{4}[A-Z0-9]{2,3})")
SNEAKER_BRANDS = [
    ("ジョーダン", "ナイキ"), ("ナイキ", "ナイキ"), ("アディダス", "アディダス"),
    ("ニューバランス", "ニューバランス"), ("アシックス", "アシックス"), ("コンバース", "コンバース"),
    ("プーマ", "プーマ"), ("ヴァンズ", "ヴァンズ"), ("リーボック", "リーボック"), ("サロモン", "サロモン"),
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


def sneaker_brand(name):
    for word, brand in SNEAKER_BRANDS:
        if word in name:
            return brand
    return "その他"


def fetch_sneakerwars(now):
    """スニーカーウォーズのトップに載っている発売予定スニーカーから、抽選の行だけ集める。"""
    top = BeautifulSoup(requests.get(SNEAKERWARS_TOP, headers=SNEAKERWARS_HEADERS, timeout=30).content, "html5lib")
    names = {}
    for desc in top.select("li .card-description"):
        link = desc.find_parent("li").find("a", href=re.compile(r"/items/view/\d+"))
        name = desc.get_text(strip=True)
        if link and "リーク" not in name:
            names.setdefault(re.search(r"/items/view/(\d+)", link["href"]).group(1), name)

    items = []
    for item_id, name in list(names.items())[:40]:
        time.sleep(1)
        page_url = f"https://sneakerwars.jp/items/view/{item_id}"
        try:
            page = BeautifulSoup(requests.get(page_url, headers=SNEAKERWARS_HEADERS, timeout=30).content, "html5lib")
        except requests.RequestException as e:
            print(f"スニーカーウォーズ {item_id} 取得エラー: {e}", file=sys.stderr)
            continue
        page_text = page.get_text(" ")
        code = STYLE_CODE.search(page_text)
        retail = re.search(r"国内価格.{0,200}?([\d,]{4,})\s*円", page_text, re.S)
        box = page.find(id="releasedata")
        for row in box.select("li") if box else []:
            shop, info = row.select_one(".font-releaseshop"), row.select_one(".font-online")
            if not shop or not info or "抽選" not in info.get_text():
                continue
            info = info.get_text(" ", strip=True)  # 例: 9/19 9:00~10/8 8:59 WEB抽選
            start_text, _, end_text = re.sub("[〜～]", "~", info).partition("~")
            link = row.find("a", href=True)
            items.append({
                "product": name,
                "shop": shop.get_text(strip=True),
                "source": "スニーカーウォーズ",
                "method": "店頭抽選" if "店頭" in info else "WEB抽選",
                "start": parse_date(start_text, now, is_end=False),
                "end": parse_date(end_text, now, is_end=True),
                "announce": "",
                "condition": info,
                "url": ("https:" + link["href"]) if link and link["href"].startswith("//") else (link["href"] if link else page_url),
                "upcoming_section": False,
                "category": "スニーカー",
                "game": sneaker_brand(name),
                "style_code": code.group(1) if code else None,
                "retail": int(retail.group(1).replace(",", "")) if retail else None,
            })
    return items


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
    jobs = [
        (game, lambda cat=cat, game=game, num=num: [
            dict(normalize_nyuka(e, now), category=cat, game=game)
            for e in fetch_nyuka_now_entries(f"https://nyuka-now.com/archives/{num}").values()
        ])
        for cat, game, num in NYUKA_PAGES
    ]
    jobs.append(("ポケカ", lambda: [
        dict(normalize_gamepedia(e, now), category="トレカ", game="ポケカ") for e in fetch_gamepedia_entries().values()
    ]))
    jobs.append(("スニーカー", lambda: fetch_sneakerwars(now)))
    for name, job in jobs:
        try:
            raw += job()
        except Exception as e:  # 1つのページが落ちても他は表示する
            errors.append(f"{name} の情報の一部が取れませんでした（{e.__class__.__name__}）")
            print(f"{name}取得エラー: {e}", file=sys.stderr)
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
    try:
        attach_prices(items, now)
    except Exception as e:  # 相場が取れなくても一覧は出す
        errors.append(f"相場の取得に失敗しました（{e.__class__.__name__}）")
        print(f"相場取得エラー: {e}", file=sys.stderr)
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
