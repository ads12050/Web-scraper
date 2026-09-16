import os, json, time, asyncio, requests
import pandas as pd
import pdfplumber
from urllib.parse import urljoin, urlparse, quote
from playwright.async_api import async_playwright

BASE_URL = "https://lightingstores.com.sa"
SEARCH_TERM = "شرائط الاضاءة"
OUTPUT_DIR = "output"
FILES_DIR = os.path.join(OUTPUT_DIR, "files")
os.makedirs(FILES_DIR, exist_ok=True)

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"}


async def collect_product_links(page):
    print("[1/5] Searching products...")
    search_url = f"{BASE_URL}/?s={quote(SEARCH_TERM)}&post_type=product"
    await page.goto(search_url, wait_until="networkidle", timeout=60000)
    await page.wait_for_timeout(3000)
    links = set()
    for _ in range(15):
        items = await page.query_selector_all("a[href*='/product/'], a[href*='/products/']")
        for item in items:
            href = await item.get_attribute("href")
            if href:
                links.add(href.split("?")[0])
        await page.mouse.wheel(0, 5000)
        await page.wait_for_timeout(1500)
    print(f"    Found {len(links)} links")
    return sorted(links)


async def extract_product_data(page, url):
    data = {"link": url, "name": "", "price": "", "stock": "", "desc": "", "specs": {}, "images": [], "files": []}
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2500)
        t = await page.query_selector("h1")
        if t: data["name"] = (await t.inner_text()).strip()
        p = await page.query_selector(".price, .woocommerce-Price-amount, [class*=price]")
        if p: data["price"] = (await p.inner_text()).strip()
        s = await page.query_selector(".stock, .availability")
        if s: data["stock"] = (await s.inner_text()).strip()
        d = await page.query_selector(".woocommerce-product-details__short-description, #tab-description, [class*=description]")
        if d: data["desc"] = (await d.inner_text()).strip()[:2000]
        rows = await page.query_selector_all("table tr")
        for row in rows:
            cells = await row.query_selector_all("th, td")
            if len(cells) >= 2:
                k = (await cells[0].inner_text()).strip()
                v = (await cells[1].inner_text()).strip()
                if k and len(k) < 100: data["specs"][k] = v
        imgs = await page.query_selector_all("img")
        for img in imgs:
            src = await img.get_attribute("src") or await img.get_attribute("data-src")
            if src and src.startswith("http") and src not in data["images"]:
                data["images"].append(src)
        exts = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".dwg")
        for a in await page.query_selector_all("a[href]"):
            href = await a.get_attribute("href")
            if href and href.lower().endswith(exts):
                full = urljoin(BASE_URL, href)
                if full not in data["files"]: data["files"].append(full)
    except Exception as e:
        print(f"    Error: {e}")
    return data


def download_file(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=60, stream=True)
        r.raise_for_status()
        name = os.path.basename(urlparse(url).path) or f"file_{int(time.time())}"
        path = os.path.join(FILES_DIR, name)
        with open(path, "wb") as f:
            for c in r.iter_content(8192): f.write(c)
        print(f"    Downloaded: {name}")
        return path
    except Exception as e:
        print(f"    Download failed: {e}")
        return None


def analyze_pdf(path):
    res = {"text": "", "tables": []}
    try:
        with pdfplumber.open(path) as pdf:
            for pg in pdf.pages:
                res["text"] += (pg.extract_text() or "") + "\n"
                for tbl in pg.extract_tables(): res["tables"].append(tbl)
    except Exception as e:
        res["text"] = f"Error: {e}"
    return res


def analyze_file(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return analyze_pdf(path)
    if ext in (".xls", ".xlsx"):
        try:
            df = pd.read_excel(path)
            return {"text": df.to_string(), "tables": [df.values.tolist()]}
        except Exception as e:
            return {"text": f"Error: {e}", "tables": []}
    return {"text": "(unsupported)", "tables": []}


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=HEADERS["User-Agent"], locale="ar-SA")
        page = await ctx.new_page()
        links = await collect_product_links(page)
        if not links:
            print("No products found.")
            await browser.close()
            return
        with open(os.path.join(OUTPUT_DIR, "links.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(links))
        products = []
        for i, link in enumerate(links, 1):
            print(f"[2/5] Product {i}/{len(links)}")
            products.append(await extract_product_data(page, link))
            await page.wait_for_timeout(1500)
        with open(os.path.join(OUTPUT_DIR, "products_raw.json"), "w", encoding="utf-8") as f:
            json.dump(products, f, ensure_ascii=False, indent=2)
        await browser.close()

    print("[3/5] Downloading files...")
    all_files = []
    for prod in products:
        for fu in prod["files"]:
            path = download_file(fu)
            if path: all_files.append({"product": prod["name"], "file": path, "url": fu})

    print("[4/5] Analyzing files...")
    file_data = []
    for fd in all_files:
        file_data.append({**fd, **analyze_file(fd["file"])})

    print("[5/5] Building Excel table...")
    rows = []
    for pr in products:
        row = {"الاسم": pr["name"], "السعر": pr["price"], "التوفر": pr["stock"],
               "الرابط": pr["link"], "عدد الصور": len(pr["images"]), "عدد الملفات": len(pr["files"])}
        for k, v in pr["specs"].items(): row[k] = v
        rows.append(row)
    df = pd.DataFrame(rows)
    df.to_excel(os.path.join(OUTPUT_DIR, "study_table.xlsx"), index=False)
    df.to_csv(os.path.join(OUTPUT_DIR, "study_table.csv"), index=False, encoding="utf-8-sig")
    if file_data:
        with open(os.path.join(OUTPUT_DIR, "file_analysis.json"), "w", encoding="utf-8") as f:
            json.dump(file_data, f, ensure_ascii=False, indent=2, default=str)
    print(f"Done! Products: {len(products)} | Files: {len(all_files)}")


if __name__ == "__main__":
    asyncio.run(main())
