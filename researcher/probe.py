"""一時的な点検用（ベストベンチャー100の取り方の確認）。"""
import sys, requests
sys.path.insert(0, ".")
from researcher.fetch import fetch
import researcher.build_bv100 as m
for u in ["https://best100.v-tsushin.jp/", "https://best100.v-tsushin.jp/company/", "https://best100.v-tsushin.jp/company/category/ad/"]:
    p = fetch(u)
    print("PAGE", u, p["status"], p.get("method"), len(p.get("links", [])))
    for t, h in p.get("links", [])[:400]:
        if "v-tsushin" in h and "/company" in h:
            print("  L", h, "|", t[:30])
    print("  members", sorted(m._members(p))[:20], "lists", m._list_pages(p, lambda h: m.SITE in h)[:10])
for params in [{"url": "best100.v-tsushin.jp/*", "from": "2019", "to": "2019", "output": "json", "limit": "20"},
               {"url": "best100.v-tsushin.jp", "matchType": "domain", "from": "2019", "to": "2019", "limit": "20"}]:
    try:
        r = requests.get("https://web.archive.org/cdx/search/cdx", params=params, timeout=90)
        print("CDX", r.status_code, r.url, r.text[:1500])
    except Exception as e:
        print("CDX ERR", repr(e))
try:
    r = requests.get("https://web.archive.org/web/2019/https://best100.v-tsushin.jp/", timeout=90)
    print("WB", r.status_code, r.url, len(r.text), r.text.count("/company/"))
except Exception as e:
    print("WB ERR", repr(e))
