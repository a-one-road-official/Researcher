"""一時的な点検用（ベストベンチャー100の取り方の確認）。"""
import requests, re, collections
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}
r = requests.get("https://best100.v-tsushin.jp/wp-sitemap-posts-company-1.xml", headers=UA, timeout=30)
locs = re.findall(r"<loc>([^<]+)</loc>", r.text); lm = re.findall(r"<lastmod>([^<]+)</lastmod>", r.text)
print("SITEMAP", r.status_code, len(locs), collections.Counter(x[:4] for x in lm))
for base in ["company", "companies"]:
    r = requests.get(f"https://best100.v-tsushin.jp/wp-json/wp/v2/{base}", params={"per_page": 100, "orderby": "date", "order": "asc", "_fields": "id,date,slug,title,company_category,status"}, headers=UA, timeout=30)
    print("REST", base, r.status_code, r.headers.get("X-WP-Total"), r.headers.get("X-WP-TotalPages"))
    if r.status_code == 200:
        js = r.json(); print([ (j["date"][:10], j["slug"]) for j in js[:15]])
r = requests.get("https://best100.v-tsushin.jp/wp-json/wp/v2/company", params={"per_page": 100, "page": 1, "_fields": "date"}, headers=UA, timeout=30)
tot = int(r.headers.get("X-WP-TotalPages") or 1); years = collections.Counter()
for pg in range(1, tot + 1):
    rr = requests.get("https://best100.v-tsushin.jp/wp-json/wp/v2/company", params={"per_page": 100, "page": pg, "_fields": "date"}, headers=UA, timeout=30)
    if rr.status_code == 200:
        years.update(j["date"][:4] for j in rr.json())
print("YEARS", sorted(years.items()))
r = requests.get("https://best100.v-tsushin.jp/wp-json/wp/v2/types/company", headers=UA, timeout=30); print("TYPE", r.status_code, r.text[:600])
