"""定価・相場・当選率の目安を集めて、抽選ごとの期待値計算に使うデータを作る。

- 定価: スニダンの商品ページの「定価」（無ければスニーカーウォーズの「国内価格」）
- 相場: スニダン（snkrdunk.com）の検索結果に出る価格。確実に同じ商品と分かったときだけ使う
- 当選率: 公開データが無いので、抽選の種類ごとの目安（推定）
"""
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

CACHE_FILE = Path(os.environ.get("PRICE_CACHE") or Path(__file__).with_name("price_cache.json"))
MARKET_TTL = timedelta(hours=6)  # 相場はこの時間ごとに取り直す
HISTORY_DAYS = 30
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36"}
SNKRDUNK_SEARCH = "https://snkrdunk.com/search?keywords="

# 商品名から取り除く「シリーズ名」など（相場検索のキーワードを作るため）
SERIES_WORDS = re.compile(
    r"ポケモンカードゲーム|ポケモンカード|ONE ?PIECE ?カードゲーム|ドラゴンボールスーパーカードゲーム|フュージョンワールド"
    r"|遊戯王(?:OCG)?|デュエル・?マスターズ|ユニオンアリーナ|UNION ARENA|MEGA|拡張パック|強化拡張パック|ハイクラスパック"
    r"|ブースターパック|プレミアムブースター|ボックス|BOX|各種|新弾"
)
SERIES_SPLIT = re.compile(r"\s(?=ポケモンカード|ONE ?PIECE|ドラゴンボール|遊戯王|デュエル|ユニオンアリーナ)")

# 当選率の目安（推定）。上から順に当てはめる
BIG_SHOPS = re.compile(
    r"Amazon|ポケモンセンター|SNKRS|NIKE|adidas|CONFIRMED|楽天|ヨドバシ|ビックカメラ|プレミアムバンダイ|トイザらス"
    r"|イオン|セブン|ローソン|ファミリーマート|ヤマダ|ゲオ|TSUTAYA|ドン・キホーテ|Joshin|エディオン|atmos",
    re.I,
)
WIN_RATE_RULES = [
    (lambda i: "招待" in i["method"], 0.03, "招待制（応募者がとても多い）"),
    (lambda i: "先着" in i["method"], 0.05, "先着販売（すぐ売り切れる）"),
    (lambda i: "店頭" in i["method"] and not BIG_SHOPS.search(i["shop"]), 0.20, "個人店・専門店の店頭抽選"),
    (lambda i: "店頭" in i["method"], 0.10, "大手チェーンの店頭抽選"),
    (lambda i: BIG_SHOPS.search(i["shop"]), 0.02, "大手のWEB抽選（応募者がとても多い）"),
    (lambda i: True, 0.05, "WEB抽選"),
]
CONDITION_BONUS = re.compile(r"会員|購入実績|レシート|アプリ|来店")


def estimate_win_rate(item):
    for rule, rate, reason in WIN_RATE_RULES:
        if rule(item):
            if CONDITION_BONUS.search(item.get("condition") or ""):
                return min(rate * 1.5, 0.5), reason + "・応募条件あり（ライバルが減る）"
            return rate, reason
    return 0.05, "WEB抽選"


# ---------- 保存ファイル ----------

def load_cache():
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_cache(cache):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- 検索キーワード ----------

def norm(text):
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"[\s「」『』\"“”・:：\-ー()（）]", "", text)


def search_keyword(product):
    """『ポケモンカード 30th CELEBRATION BOX ポケモンカード …』→『30th CELEBRATION』"""
    first = SERIES_SPLIT.split(unicodedata.normalize("NFKC", product).strip())[0]
    first = re.sub(r"【[^】]*】|\([^)]*\)", " ", first)
    return re.sub(r"\s+", " ", SERIES_WORDS.sub(" ", first)).strip()


# ---------- 相場（スニダン） ----------

def snkrdunk_search(keyword):
    html = requests.get(SNKRDUNK_SEARCH + urllib.parse.quote(keyword), headers=HEADERS, timeout=30).text
    results = []
    for a in BeautifulSoup(html, "html5lib").find_all("a", attrs={"aria-label": re.compile(r" - ¥[\d,]+$")}):
        name, _, price = a["aria-label"].rpartition(" - ¥")
        results.append({"name": name, "price": int(price.replace(",", "")), "url": a["href"]})
    return results


def find_market(item):
    """スニダンで同じ商品を探す。見つからない・自信がないときは None。"""
    if item.get("style_code"):  # スニーカーは品番で完全一致させる
        code = item["style_code"]
        for r in snkrdunk_search(code):
            if r["url"].rstrip("/").endswith("/" + code):
                return r
        return None

    keyword = search_keyword(item["product"])
    if len(norm(keyword)) < 3:
        return None
    key = norm(keyword)
    candidates = [
        r for r in snkrdunk_search(keyword)
        if key in norm(r["name"]) and "シュリンクなし" not in r["name"] and "[" not in r["name"]
        and "/apparels/" in r["url"] and "カートン" not in r["name"]
    ]
    wants_pack = "パック" in item["product"] and "BOX" not in item["product"].upper() and "ボックス" not in item["product"]
    # 抽選の商品に合わせて、ボックスかパックかを選ぶ
    for r in candidates:
        if r["name"].endswith("パック") == wants_pack:
            return r
    return None


# ---------- 定価 ----------

def fetch_snkrdunk_retail(url):
    text = BeautifulSoup(requests.get(url, headers=HEADERS, timeout=30).text, "html5lib").get_text(" ")
    m = re.search(r"定価\s*¥\s*([\d,]{3,})", text)
    return int(m.group(1).replace(",", "")) if m else None


# ---------- まとめ ----------

def price_key(item):
    return item.get("style_code") or f"{item['category']}|{norm(search_keyword(item['product']))}"


def attach_prices(items, now):
    """各抽選に retail / market / winRate などを付け足す。"""
    cache = load_cache()
    today = now.date().isoformat()
    for item in items:
        key = price_key(item)
        entry = cache.setdefault(key, {"history": []})

        checked = entry.get("market_checked")
        if not checked or now - datetime.fromisoformat(checked) > MARKET_TTL:
            try:
                market = find_market(item)
                time.sleep(1)
                entry["market_checked"] = now.isoformat()
                entry["market"] = market
                if market and entry.get("retail_url") != market["url"]:
                    entry["retail"] = fetch_snkrdunk_retail(market["url"])  # 定価は変わらないので1回だけ
                    entry["retail_url"] = market["url"]
                    time.sleep(1)
                if market:
                    history = [h for h in entry["history"] if h[0] != today]
                    entry["history"] = (history + [[today, market["price"]]])[-HISTORY_DAYS:]
            except requests.RequestException as e:
                print(f"相場の取得エラー: {e}", file=sys.stderr)

        rate, reason = estimate_win_rate(item)
        item["retail"] = entry.get("retail") or item.get("retail")
        item["market"] = entry.get("market")
        item["priceHistory"] = entry["history"]
        item["winRate"] = rate
        item["winRateReason"] = reason

    # 使われなくなった商品は60日で消す
    cutoff = (now - timedelta(days=60)).isoformat()
    for key in [k for k, v in cache.items() if (v.get("market_checked") or now.isoformat()) < cutoff]:
        del cache[key]
    save_cache(cache)
