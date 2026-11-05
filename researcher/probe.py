"""一時的な点検用（ベストベンチャー100の取り方の確認）。"""
import sys, re, time, requests
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}
for u in ["https://best100.v-tsushin.jp/robots.txt", "https://best100.v-tsushin.jp/sitemap.xml", "https://best100.v-tsushin.jp/wp-sitemap.xml",
          "https://best100.v-tsushin.jp/sitemap_index.xml", "https://best100.v-tsushin.jp/wp-json/wp/v2/types"]:
    try:
        r = requests.get(u, headers=UA, timeout=30)
        print("GET", u, r.status_code, len(r.text)); print(r.text[:1200])
    except Exception as e:
        print("ERR", u, e)
r = requests.get("https://best100.v-tsushin.jp/company/yaaha/", headers=UA, timeout=30)
for k in ["article:published_time", "article:modified_time", "datePublished", "dateModified", "掲載", "年度", "選出"]:
    for m in re.finditer(re.escape(k), r.text):
        print("META", k, r.text[max(0, m.start()-80): m.end()+120].replace("\n", " ")); break
time.sleep(5)
for params in [{"url": "best100.v-tsushin.jp/company/*", "output": "json", "limit": "30", "fl": "timestamp,original,statuscode"},
               {"url": "best100.v-tsushin.jp/", "output": "json", "limit": "30", "fl": "timestamp,original,statuscode"}]:
    time.sleep(8)
    try:
        r = requests.get("https://web.archive.org/cdx/search/cdx", params=params, headers=UA, timeout=120)
        print("CDX", r.status_code, r.text[:1500])
    except Exception as e:
        print("CDX ERR", repr(e))
