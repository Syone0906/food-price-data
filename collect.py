# -*- coding: utf-8 -*-
"""東京都中央卸売市場の日報（青果・9市場）を集めて、日付×品名×産地の円/kg にまとめる。

GitHub Actions で毎日実行される想定。標準ライブラリのみ。
出力（data フォルダ）:
  YYYY-MM.csv   月ごとの日次データ（スプレッドシートの「④ 過去分」が読む）
  recent.csv    直近14日分（スプレッドシートの毎日の更新が読む）
"""
import csv, io, os, random, sys, time, urllib.request, urllib.error
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

BASE_URL  = "https://www.shijou-nippo.metro.tokyo.lg.jp"
DAYS_BACK = int(os.environ.get("DAYS_BACK") or "12")
WORKERS   = 4      # 出典サーバーは同時接続が多いと接続を切るので控えめに
RETRIES   = 4
MARKETS   = range(1, 10)   # K1..K9 = 豊洲・大田・豊島・淀橋・板橋・世田谷・北足立・多摩NT・葛西
JST       = timezone(timedelta(hours=9))
ROOT      = os.path.dirname(os.path.abspath(__file__))
DATA      = os.path.join(ROOT, "data")
HEAD      = ["日付", "年月", "品名", "産地", "円/kg（代表値平均）", "円/kg（高値）", "円/kg（安値）", "件数", "市場数"]
DASHES    = set("-−－ー―")
SKIP_ITEM = {"品名", "小計", "合計", "全分類合計"}


def fetch(url):
    """200 → 本文 / 404 → 'CLOSED'（休市・未掲載）/ それ以外 → None（取得失敗）"""
    req = urllib.request.Request(url, headers={"User-Agent": "food-price-tracker/1.0"})
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                if r.status == 200:
                    return r.read().decode("cp932", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return "CLOSED"
        except Exception:
            pass
        time.sleep(1.0 * (attempt + 1) + random.random())
    return None


def is_dash(s):
    s = (s or "").strip()
    return bool(s) and all(c in DASHES for c in s)


def num(v):
    s = (v or "").replace(",", "").replace("，", "").strip()
    if not s or is_dash(s):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse(text):
    """日報CSVを解析して (市場名, [(品名, 産地, 代表円/kg, 高値円/kg, 安値円/kg), ...]) を返す"""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return "", []
    first = "".join(rows[0])
    market = first.split("・")[-1].split("）")[0].strip() if "・" in first and "）" in first else ""
    carry, recs = ["", "", "", ""], []
    for row in rows:
        if len(row) < 10:
            continue
        for i in range(4):
            if row[i].strip():
                carry[i] = row[i].strip()
        item = carry[0]
        if not item or is_dash(item) or item in SKIP_ITEM:
            continue
        origin = row[5].strip()
        unit = num(row[6])
        high, mid, low = num(row[7]), num(row[8]), num(row[9])
        if not unit or unit <= 0:
            continue
        rep = mid
        if rep is None and high is not None and low is not None:
            rep = (high + low) / 2
        if rep is None:
            rep = high if high is not None else low
        if rep is None:
            continue
        recs.append((item, origin if origin and not is_dash(origin) else "不明",
                     rep / unit,
                     high / unit if high is not None else None,
                     low / unit if low is not None else None))
    return market, recs


def r0(x):
    return int(x + 0.5)


def collect():
    today = datetime.now(JST).date()
    jobs = [((today - timedelta(days=b)).strftime("%Y%m%d"), k)
            for b in range(0, DAYS_BACK + 1) for k in MARKETS]

    def one(job):
        ymd, k = job
        return ymd, fetch("%s/SN/%s/%s/Sei/Sei_K%d.csv" % (BASE_URL, ymd[:6], ymd, k))

    pages, failed = defaultdict(list), set()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for ymd, text in pool.map(one, jobs):
            if text is None:
                failed.add(ymd)
            elif text != "CLOSED":
                pages[ymd].append(text)

    by_date = {}
    for ymd, texts in pages.items():
        if ymd in failed:
            continue                       # 一部の市場が取れなかった日は上書きしない
        agg = {}
        for t in texts:
            market, recs = parse(t)
            if not market:
                continue
            for item, origin, per, hi, lo in recs:
                a = agg.setdefault((item, origin), {"sum": 0.0, "n": 0, "hi": None, "lo": None, "mk": set()})
                a["sum"] += per
                a["n"] += 1
                h = hi if hi is not None else per
                l = lo if lo is not None else per
                a["hi"] = h if a["hi"] is None else max(a["hi"], h)
                a["lo"] = l if a["lo"] is None else min(a["lo"], l)
                a["mk"].add(market)
        iso = "%s-%s-%s" % (ymd[:4], ymd[4:6], ymd[6:])
        by_date[iso] = [[iso, iso[:7], item, origin, r0(a["sum"] / a["n"]), r0(a["hi"]), r0(a["lo"]),
                         a["n"], len(a["mk"])] for (item, origin), a in agg.items()]

    print("取得できた日: %d / 休市・未掲載: %d / 通信エラーで見送り: %d（%s）"
          % (len(by_date), DAYS_BACK + 1 - len(by_date) - len(failed), len(failed), ",".join(sorted(failed))))
    if not by_date and failed:
        sys.exit("1日分も取得できませんでした。数分おいて再実行してください。")
    return by_date


def read_month(path):
    if not os.path.exists(path):
        return []
    with io.open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    return rows[1:]


def write_csv(path, rows):
    rows.sort(key=lambda r: (r[0], r[2], r[3]))
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEAD)
        w.writerows(rows)


def save(by_date):
    os.makedirs(DATA, exist_ok=True)
    months = defaultdict(dict)
    for iso, rows in by_date.items():
        months[iso[:7]][iso] = rows
    for ym, days in months.items():
        path = os.path.join(DATA, ym + ".csv")
        kept = [r for r in read_month(path) if r[0] not in days]
        for rows in days.values():
            kept += rows
        write_csv(path, kept)
        print("  %s.csv: %d 行" % (ym, len(kept)))

    # 直近14日分
    cutoff = (datetime.now(JST).date() - timedelta(days=14)).isoformat()
    recent = []
    month_files = sorted(n for n in os.listdir(DATA) if n.endswith(".csv") and n[:4].isdigit())
    for name in month_files[-2:]:
        recent += [r for r in read_month(os.path.join(DATA, name)) if r[0] >= cutoff]
    write_csv(os.path.join(DATA, "recent.csv"), recent)
    print("  recent.csv: %d 行" % len(recent))


if __name__ == "__main__":
    save(collect())
