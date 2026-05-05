#!/usr/bin/env python3
"""
Comparador de Preços: Tauste vs Coop (Sorocaba)
Analisa hortifruti e açougue pensando em um casal.

Requisitos:
    pip install requests beautifulsoup4 playwright
    playwright install chromium
"""

import os
import re
import json
import statistics
import sys
from datetime import datetime

# Garante que o terminal Windows aceita UTF-8
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ─────────────────────────────────────────────────────────────
# UTILITÁRIOS
# ─────────────────────────────────────────────────────────────

def parse_price(text: str) -> float | None:
    """Extrai valor numérico de strings como 'R$ 6,98' ou '6.98'."""
    text = text.strip()
    # Remove R$, espaços, pontos de milhar; troca vírgula decimal por ponto
    cleaned = re.sub(r"[R$\s]", "", text).replace(".", "").replace(",", ".")
    try:
        value = float(cleaned)
        # Sanity check: preços razoáveis entre R$ 0,50 e R$ 2.000
        return value if 0.5 <= value <= 2000 else None
    except ValueError:
        return None


def extract_weight_kg(name: str) -> float | None:
    """Tenta extrair o peso em kg do nome do produto."""
    name_lower = name.lower()
    kg_match = re.search(r"(\d+[,.]?\d*)\s*kg", name_lower)
    g_match = re.search(r"(\d+)\s*g\b", name_lower)
    if kg_match:
        return float(kg_match.group(1).replace(",", "."))
    if g_match:
        return int(g_match.group(1)) / 1000
    return None


# ─────────────────────────────────────────────────────────────
# TAUSTE (Magento — HTML estático, funciona com requests)
# ─────────────────────────────────────────────────────────────

def _scrape_tauste_page(url: str) -> list[dict]:
    """Raspa uma página de listagem do Tauste."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"    [Tauste] Erro ao acessar {url}: {exc}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    products = []

    for li in soup.find_all("li"):
        # Cada produto tem ≥2 <strong> — o último é o nome do produto
        strongs = li.find_all("strong")
        if len(strongs) < 2:
            continue

        name = strongs[-1].get_text(strip=True)
        if not name or len(name) < 4:
            continue

        # O preço aparece como texto direto no <li> após os <strong>
        li_text = li.get_text(separator=" ", strip=True)
        price_match = re.search(r"R\$\s*(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2}))", li_text)
        if not price_match:
            continue

        price = parse_price(price_match.group(1))
        if price is None:
            continue

        products.append({"name": name, "price": price, "market": "Tauste"})

    return products


def scrape_tauste(base_url: str, label: str, max_pages: int = 10) -> list[dict]:
    """Raspa todas as páginas de uma categoria do Tauste."""
    all_products: list[dict] = []
    for page in range(1, max_pages + 1):
        url = f"{base_url}?p={page}" if page > 1 else base_url
        print(f"    → Página {page}: {url}")
        items = _scrape_tauste_page(url)
        if not items:
            print(f"    → Sem produtos na página {page}, encerrando.")
            break
        all_products.extend(items)
        print(f"      {len(items)} produtos coletados (total: {len(all_products)})")
        # Se veio menos que 40 produtos, provavelmente é a última página
        if len(items) < 40:
            break
    return all_products


# ─────────────────────────────────────────────────────────────
# COOP (VTEX — tenta API pública, cai para Playwright)
# ─────────────────────────────────────────────────────────────

# IDs de categoria no VTEX do Coop (obtidos de /api/catalog_system/pub/category/tree/2)
COOP_CATEGORY_IDS = {
    "hortifruti": 154,
    "acougue": 306,
}


def _parse_vtex_product(item: dict, market: str = "Coop") -> dict | None:
    """Extrai nome e preço de um item da API VTEX."""
    name = item.get("productName") or item.get("name", "")
    if not name:
        return None

    # VTEX guarda preços em items[0].sellers[0].commertialOffer.Price
    try:
        price = item["items"][0]["sellers"][0]["commertialOffer"]["Price"]
    except (KeyError, IndexError):
        price = None

    price = float(price)
    # Preços acima de R$ 2000 ou iguais a 0 são placeholders de indisponibilidade
    if not price or price > 2000:
        return None

    return {"name": name, "price": price, "market": market}


def _scrape_coop_vtex_api(category_slug: str) -> list[dict]:
    """Raspa via API VTEX catalog_system filtrando pelo ID de categoria."""
    category_id = COOP_CATEGORY_IDS.get(category_slug)
    if not category_id:
        print(f"    → ID de categoria não mapeado para '{category_slug}'.")
        return []

    all_products: list[dict] = []
    batch_size = 49  # _from=0&_to=49 retorna 50 items
    start = 0
    total: int | None = None

    vtex_headers = {**HEADERS, "Accept": "application/json"}

    while True:
        url = (
            f"https://www.coopsupermercado.com.br/api/catalog_system/pub/products/search"
            f"?fq=C:{category_id}&_from={start}&_to={start + batch_size}"
        )
        print(f"    → API VTEX [{start}-{start+batch_size}]: {url[:90]}...")
        try:
            resp = requests.get(url, headers=vtex_headers, timeout=20)
            # VTEX retorna 206 (Partial Content) para paginação — é normal
            if resp.status_code not in (200, 206):
                print(f"    → Status inesperado {resp.status_code}.")
                return []

            # Cabeçalho "resources" informa o total: "0-49/146"
            if total is None:
                resources = resp.headers.get("resources", "")
                match = re.search(r"/(\d+)$", resources)
                if match:
                    total = int(match.group(1))
                    print(f"    → Total de produtos na categoria: {total}")

            data = resp.json()
        except Exception as exc:
            print(f"    → Erro na API VTEX: {exc}")
            return []

        if not data:
            break

        for item in data:
            parsed = _parse_vtex_product(item)
            if parsed:
                all_products.append(parsed)

        print(f"      {len(data)} itens recebidos (total: {len(all_products)})")

        start += batch_size + 1
        if total is not None and start >= total:
            break
        if len(data) < batch_size:
            break

    return all_products


def _scrape_coop_playwright(base_url: str) -> list[dict]:
    """Raspa o Coop via Playwright (headless browser) quando a API não está disponível."""
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        print("    [Coop] Playwright não instalado.")
        print("    Execute: pip install playwright && playwright install chromium")
        return []

    all_products: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="pt-BR",
        )
        page = context.new_page()
        page_num = 1

        while True:
            url = f"{base_url}?page={page_num}" if page_num > 1 else base_url
            print(f"    → Playwright página {page_num}: {url}")

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                # Aguardar que algum produto apareça (VTEX pode demorar)
                page.wait_for_selector(
                    "h3, [class*='productName'], [class*='product-name'], "
                    "[class*='galleryItem'], [class*='ProductCard']",
                    timeout=20_000,
                )
            except PWTimeout:
                print(f"    → Timeout na página {page_num}, encerrando.")
                break

            html = page.content()
            soup = BeautifulSoup(html, "html.parser")

            # VTEX usa <h3> para nomes e elementos com "price" para preços
            # Tenta localizar pares nome+preço dentro de cards de produto
            page_products: list[dict] = []

            product_cards = soup.select(
                "[class*='productCard'], [class*='product-card'], "
                "[class*='ProductCard'], article, [class*='shelf']"
            )

            if not product_cards:
                # Fallback: tenta extrair de todo o texto da página
                names = [el.get_text(strip=True) for el in soup.find_all("h3") if el.get_text(strip=True)]
                prices_raw = re.findall(r"R\$\s*(\d{1,3}(?:[.,]\d{3})*[.,]\d{2})", html)
                for name, price_raw in zip(names, prices_raw):
                    price = parse_price(price_raw)
                    if price and len(name) > 3:
                        page_products.append({"name": name, "price": price, "market": "Coop"})
            else:
                for card in product_cards:
                    name_el = card.select_one("h3, [class*='productName'], [class*='product-name']")
                    price_text = card.get_text(separator=" ")
                    price_match = re.search(r"R\$\s*(\d{1,3}(?:[.,]\d{3})*[.,]\d{2})", price_text)
                    if name_el and price_match:
                        name = name_el.get_text(strip=True)
                        price = parse_price(price_match.group(1))
                        if price and len(name) > 3:
                            page_products.append({"name": name, "price": price, "market": "Coop"})

            if not page_products:
                print(f"    → Nenhum produto encontrado na página {page_num}, encerrando.")
                break

            all_products.extend(page_products)
            print(f"      {len(page_products)} produtos coletados (total: {len(all_products)})")

            # Verificar botão "próxima página"
            next_btn = page.query_selector("a[rel='next'], [aria-label='Próxima página'], .pagination-next")
            if not next_btn:
                break
            page_num += 1

        browser.close()

    return all_products


def scrape_coop(category_url: str, category_slug: str, label: str) -> list[dict]:
    """Raspa uma categoria do Coop: tenta API VTEX primeiro, depois Playwright."""
    print(f"    Tentando API VTEX para '{label}'...")
    products = _scrape_coop_vtex_api(category_slug)
    if products:
        return products

    print(f"    API indisponível, usando Playwright para '{label}'...")
    return _scrape_coop_playwright(category_url)


# ─────────────────────────────────────────────────────────────
# CONFIANÇA (Oracle Commerce Cloud — Playwright + API OCC)
# ─────────────────────────────────────────────────────────────

def scrape_confianca(category_url: str, label: str) -> list[dict]:
    """
    Raspa uma categoria do Confiança via Playwright.
    Intercepta chamadas a ccstore/v1/products?productIds=... para obter
    nome (displayName) e preço (childSKUs[0].salePrice | listPrice).
    Clica em 'Carregar mais' até esgotar os produtos.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("    [Confianca] Playwright não instalado.")
        return []

    collected: dict[str, dict] = {}

    def intercept(response):
        url = response.url
        if "ccstore/v1/products" in url and "productIds" in url and response.status == 200:
            try:
                body = response.json()
                for item in body.get("items", []):
                    pid = item.get("id")
                    name = item.get("displayName", "").strip()
                    if not pid or not name:
                        continue
                    price = None
                    for sku in item.get("childSKUs", []):
                        price = sku.get("salePrice") or sku.get("listPrice")
                        if price:
                            break
                    if price and 0.5 <= float(price) <= 2000:
                        collected[pid] = {
                            "name": name,
                            "price": float(price),
                            "market": "Confianca",
                        }
            except Exception:
                pass

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=HEADERS["User-Agent"], locale="pt-BR")
        page = ctx.new_page()
        page.on("response", intercept)

        print(f"    → Carregando {category_url[:80]}...")
        try:
            page.goto(category_url, wait_until="networkidle", timeout=60_000)
        except Exception:
            pass

        print(f"    → {len(collected)} produtos após carregamento inicial")

        # Clica em "Carregar mais" até não aparecer ou não trazer novos produtos
        for i in range(30):
            btn = page.query_selector(
                'button:has-text("Carregar mais"), a:has-text("Carregar mais")'
            )
            if not btn:
                break
            prev = len(collected)
            try:
                btn.click()
                page.wait_for_timeout(3_000)
            except Exception:
                break
            gained = len(collected) - prev
            print(f"    → Clique {i + 1}: +{gained} produtos (total: {len(collected)})")
            if gained == 0:
                break

        browser.close()

    return list(collected.values())


# ─────────────────────────────────────────────────────────────
# ANÁLISE E RELATÓRIO
# ─────────────────────────────────────────────────────────────

def analyze(products: list[dict], market: str, category: str) -> dict | None:
    if not products:
        return None
    prices = [p["price"] for p in products]
    cheapest = min(products, key=lambda x: x["price"])
    priciest = max(products, key=lambda x: x["price"])
    return {
        "market": market,
        "category": category,
        "count": len(products),
        "mean": statistics.mean(prices),
        "median": statistics.median(prices),
        "min": min(prices),
        "max": max(prices),
        "cheapest_name": cheapest["name"],
        "cheapest_price": cheapest["price"],
        "priciest_name": priciest["name"],
        "priciest_price": priciest["price"],
    }


def print_report(results: list[tuple]) -> None:
    width = 64
    sep = "─" * width

    print()
    print("═" * width)
    print("  COMPARADOR DE MERCADOS — SOROCABA")
    print(f"  {datetime.now().strftime('%d/%m/%Y às %H:%M')}")
    print("═" * width)

    for row in results:
        category = row[0]
        stats_list = [s for s in row[1:] if s]

        print(f"\n{sep}")
        print(f"  {category.upper()}")
        print(sep)

        for stats in stats_list:
            print(f"\n  Mercado : {stats['market']}")
            print(f"  Produtos: {stats['count']}")
            print(f"  Média   : R$ {stats['mean']:.2f}")
            print(f"  Mediana : R$ {stats['median']:.2f}")
            print(f"  Barato  : {stats['cheapest_name'][:45]} — R$ {stats['cheapest_price']:.2f}")
            print(f"  Caro    : {stats['priciest_name'][:45]} — R$ {stats['priciest_price']:.2f}")

        if len(stats_list) >= 2:
            winner = min(stats_list, key=lambda s: s["mean"])
            loser_mean = max(s["mean"] for s in stats_list)
            diff = loser_mean - winner["mean"]
            pct = diff / loser_mean * 100
            print(f"\n  ★ {winner['market']} tem o menor preço médio "
                  f"({pct:.1f}% abaixo do mais caro, R$ {diff:.2f} de diferença)")

    print(f"\n{'═' * width}\n")


# ─────────────────────────────────────────────────────────────
# HISTÓRICO E GERAÇÃO DE HTML
# ─────────────────────────────────────────────────────────────

HISTORY_FILE = "historico.json"
HTML_FILE = "index.html"


def load_history() -> list[dict]:
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("snapshots", [])
    except (json.JSONDecodeError, KeyError):
        return []


def save_history(history: list[dict]) -> None:
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump({"snapshots": history}, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Histórico salvo em: {HISTORY_FILE} ({len(history)} snapshot(s))")


def snapshot_summary(collected: dict, timestamp: str) -> dict:
    """Versão compacta do snapshot para o histórico (só estatísticas + lista de produtos)."""
    keys = ["tauste_horti", "tauste_carnes", "coop_horti", "coop_acougue", "confianca_horti", "confianca_acougue"]
    stats = {}
    for key in keys:
        products = collected.get(key, [])
        prices = [p["price"] for p in products]
        stats[key] = {
            "count": len(products),
            "mean": round(statistics.mean(prices), 2) if prices else 0,
            "median": round(statistics.median(prices), 2) if prices else 0,
            "min": round(min(prices), 2) if prices else 0,
            "max": round(max(prices), 2) if prices else 0,
            "products": sorted(products, key=lambda x: x["price"])[:50],
        }
    return {"ts": timestamp, "stats": stats}


def generate_html(history: list[dict], canonical: dict | None = None, revisao_pin: str = "") -> None:
    data_json = json.dumps(history, ensure_ascii=False, separators=(",", ":"))
    canonical_json = json.dumps(canonical or {}, ensure_ascii=False, separators=(",", ":"))
    pin_js = json.dumps(revisao_pin)  # string JS devidamente quoted/escaped

    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Comparador de Mercados — Sorocaba</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root {{
  --bg:#0f172a;--surface:#1e293b;--surface2:#263248;--border:#334155;
  --text:#f1f5f9;--muted:#94a3b8;
  --tauste:#3b82f6;--coop:#10b981;--confianca:#f59e0b;
  --tauste-f:rgba(59,130,246,.15);--coop-f:rgba(16,185,129,.15);--confianca-f:rgba(245,158,11,.15);
  --green:#4ade80;--red:#f87171;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:system-ui,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}}
header{{padding:1.5rem 2rem .75rem;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:1.5rem;flex-wrap:wrap}}
header h1{{font-size:1.3rem;font-weight:700;white-space:nowrap}}
header p{{color:var(--muted);font-size:.85rem}}
nav{{display:flex;gap:.25rem;padding:.6rem 2rem;border-bottom:1px solid var(--border);background:var(--surface);position:sticky;top:0;z-index:10}}
.nav-btn{{padding:.45rem 1.1rem;border-radius:.4rem;border:none;background:transparent;color:var(--muted);cursor:pointer;font-size:.85rem;font-weight:500;transition:background .15s,color .15s}}
.nav-btn:hover{{color:var(--text)}}
.nav-btn.active{{background:var(--tauste);color:#fff}}
.page{{display:none}}
.page.active{{display:block}}
main{{max-width:1280px;margin:0 auto;padding:2rem}}
h2{{font-size:1rem;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin:2rem 0 1rem}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:1rem}}
.card{{background:var(--surface);border:1px solid var(--border);border-radius:.75rem;padding:1.25rem}}
.card-label{{font-size:.78rem;color:var(--muted);margin-bottom:.25rem}}
.card-value{{font-size:1.7rem;font-weight:700}}
.card-sub{{font-size:.78rem;color:var(--muted);margin-top:.25rem}}
.badge{{display:inline-block;padding:.18rem .5rem;border-radius:999px;font-size:.73rem;font-weight:600}}
.badge-tauste{{background:var(--tauste-f);color:var(--tauste)}}
.badge-coop{{background:var(--coop-f);color:var(--coop)}}
.badge-confianca{{background:var(--confianca-f);color:var(--confianca)}}
.charts{{display:grid;grid-template-columns:1fr 1fr;gap:1.5rem}}
@media(max-width:700px){{.charts{{grid-template-columns:1fr}}}}
.chart-box{{background:var(--surface);border:1px solid var(--border);border-radius:.75rem;padding:1.25rem}}
.chart-box h3{{font-size:.85rem;color:var(--muted);margin-bottom:1rem}}
.table-box{{background:var(--surface);border:1px solid var(--border);border-radius:.75rem;overflow:hidden;margin-bottom:1.5rem}}
.table-box h3{{font-size:.85rem;font-weight:600;padding:.85rem 1rem;border-bottom:1px solid var(--border)}}
table{{width:100%;border-collapse:collapse;font-size:.81rem}}
th{{background:#0f172a;color:var(--muted);font-weight:500;text-align:left;padding:.5rem 1rem;border-bottom:1px solid var(--border);cursor:pointer;user-select:none;white-space:nowrap}}
th:hover{{color:var(--text)}}
td{{padding:.5rem 1rem;border-bottom:1px solid var(--border);color:var(--text)}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:rgba(255,255,255,.03)}}
.price{{font-weight:600;white-space:nowrap}}
.search-bar{{padding:.75rem 1rem;border-bottom:1px solid var(--border);display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}}
.search-bar input{{flex:1;min-width:140px;background:var(--bg);border:1px solid var(--border);border-radius:.4rem;color:var(--text);padding:.4rem .75rem;font-size:.82rem;outline:none}}
.search-bar input:focus{{border-color:var(--tauste)}}
.search-bar select{{background:var(--bg);border:1px solid var(--border);border-radius:.4rem;color:var(--text);padding:.4rem .75rem;font-size:.82rem;cursor:pointer;outline:none}}
.winner-tag{{color:var(--green);font-size:.73rem;margin-left:.4rem}}
.tabs{{display:flex;gap:.5rem;margin-bottom:1rem;flex-wrap:wrap}}
.tab{{padding:.35rem .9rem;border-radius:.4rem;border:1px solid var(--border);background:transparent;color:var(--muted);cursor:pointer;font-size:.82rem}}
.tab.active{{background:var(--tauste);border-color:var(--tauste);color:#fff}}
.tab.coop.active{{background:var(--coop);border-color:var(--coop)}}
.tab.confianca.active{{background:var(--confianca);border-color:var(--confianca);color:#111}}
.no-data{{color:var(--muted);font-size:.85rem;padding:2rem;text-align:center}}
.snap-table td,.snap-table th{{padding:.4rem .75rem}}
/* Itens Comuns */
.best{{color:var(--green);font-weight:700}}
.por-kg{{color:var(--muted);font-size:.75rem;display:block}}
tr.clickable{{cursor:pointer}}
/* Modal */
.modal-overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:100;align-items:center;justify-content:center}}
.modal-overlay.open{{display:flex}}
.modal{{background:var(--surface);border:1px solid var(--border);border-radius:1rem;padding:1.75rem;max-width:720px;width:90%;max-height:85vh;overflow-y:auto}}
.modal-header{{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:1.25rem;gap:1rem}}
.modal-title{{font-size:1rem;font-weight:700}}
.modal-close{{background:transparent;border:none;color:var(--muted);cursor:pointer;font-size:1.3rem;line-height:1;padding:.1rem .4rem;border-radius:.3rem}}
.modal-close:hover{{color:var(--text)}}
/* Carrinho */
.cart-grid{{display:grid;grid-template-columns:1fr;gap:0}}
.cart-row{{display:grid;grid-template-columns:2rem 1fr repeat(3,9rem);align-items:center;gap:.5rem;padding:.55rem 1rem;border-bottom:1px solid var(--border);font-size:.82rem}}
.cart-row:last-child{{border-bottom:none}}
.cart-header{{font-size:.75rem;color:var(--muted);font-weight:500;background:#0f172a;padding:.5rem 1rem;border-bottom:1px solid var(--border)}}
.cart-header{{display:grid;grid-template-columns:2rem 1fr repeat(3,9rem);gap:.5rem}}
.cart-total{{display:grid;grid-template-columns:calc(2rem + 1fr + .5rem) repeat(3,9rem);gap:.5rem;padding:.75rem 1rem;background:var(--surface2);font-weight:700;font-size:.85rem;border-top:2px solid var(--border)}}
.radio-mkt{{display:flex;flex-direction:column;gap:.3rem;align-items:center}}
.radio-mkt label{{font-size:.72rem;color:var(--muted);display:flex;align-items:center;gap:.25rem;cursor:pointer;white-space:nowrap}}
.mkt-price{{font-size:.8rem;white-space:nowrap}}
.mkt-price.unavail{{color:var(--muted);font-style:italic;font-size:.72rem}}
.btn{{padding:.4rem .9rem;border-radius:.4rem;border:none;cursor:pointer;font-size:.8rem;font-weight:600}}
.btn-primary{{background:var(--tauste);color:#fff}}
.btn-sm{{padding:.3rem .7rem;font-size:.75rem}}
.btn-green{{background:rgba(74,222,128,.15);color:var(--green);border:1px solid rgba(74,222,128,.3)}}
.btn-red{{background:rgba(248,113,113,.15);color:var(--red);border:1px solid rgba(248,113,113,.3)}}
.score-bar{{display:inline-block;height:.35rem;border-radius:999px;vertical-align:middle;margin-left:.5rem}}
/* Disclaimer */
.disclaimer{{background:rgba(245,158,11,.07);border-bottom:1px solid rgba(245,158,11,.18);padding:.55rem 1.5rem;display:flex;align-items:center;gap:.75rem;font-size:.78rem;color:var(--muted);flex-wrap:wrap;line-height:1.5}}
.disclaimer-icon{{font-size:1rem;flex-shrink:0}}
.disclaimer-text{{flex:1;min-width:200px}}
.disclaimer-text strong{{color:#fbbf24}}
.disclaimer-close{{margin-left:auto;background:none;border:none;color:var(--muted);cursor:pointer;font-size:1.1rem;line-height:1;padding:.15rem .4rem;border-radius:.3rem;flex-shrink:0}}
.disclaimer-close:hover{{color:var(--text)}}
.disclaimer.hidden{{display:none!important}}
/* Footer */
footer{{text-align:center;padding:1.75rem 2rem 2rem;color:var(--muted);font-size:.75rem;border-top:1px solid var(--border);line-height:1.9;margin-top:1rem}}
footer a{{color:var(--muted);text-decoration:underline;text-underline-offset:2px}}
footer a:hover{{color:var(--text)}}
/* Carrinho — wrapper que usa display:contents no desktop */
.cart-row-name{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.cart-row-mkts{{display:contents}}
/* Mobile */
@media(max-width:640px){{
  header{{padding:1rem 1rem .6rem}}
  nav{{padding:.4rem .75rem;flex-wrap:wrap;gap:.2rem}}
  .nav-btn{{padding:.35rem .75rem;font-size:.78rem}}
  main{{padding:1rem}}
  h2{{margin:1.25rem 0 .75rem}}
  .cards{{grid-template-columns:1fr 1fr}}
  .tab{{font-size:.74rem;padding:.28rem .55rem}}
  .disclaimer{{padding:.5rem 1rem;gap:.5rem}}
  .cart-header{{display:none}}
  .cart-row{{display:flex;flex-wrap:wrap;gap:.4rem;padding:.75rem 1rem;align-items:center}}
  .cart-row>input[type=checkbox]{{flex:0 0 auto;width:1rem;height:1rem}}
  .cart-row-name{{flex:1 1 0;font-size:.85rem;align-self:center}}
  .cart-row-mkts{{flex:0 0 100%;display:flex;gap:.35rem}}
  .radio-mkt{{flex:1 1 0;background:var(--surface2);border-radius:.4rem;padding:.35rem .5rem;align-items:flex-start;min-width:0}}
  .radio-mkt label{{font-size:.7rem;white-space:normal}}
  .mkt-price{{font-size:.78rem}}
  .cart-total{{grid-template-columns:1fr;gap:.2rem;padding:.65rem 1rem}}
  .cart-total>span:first-child{{font-size:.75rem;color:var(--muted);font-weight:400}}
  .snap-table{{font-size:.72rem}}
  .snap-table td,.snap-table th{{padding:.35rem .5rem}}
  .modal{{padding:1.25rem;width:95%;max-height:90vh}}
  .search-bar{{gap:.35rem;padding:.6rem .75rem}}
}}
@media(max-width:400px){{
  .cards{{grid-template-columns:1fr}}
  .cart-row-mkts{{flex-direction:column}}
}}
</style>
</head>
<body>
<header>
  <div><h1>Comparador de Mercados — Sorocaba</h1><p id="subtitle">Carregando...</p></div>
</header>
<nav>
  <button class="nav-btn active" id="nav-dashboard" onclick="showPage('dashboard')">Dashboard</button>
  <button class="nav-btn" id="nav-comuns" onclick="showPage('comuns')">Itens Comuns</button>
  <button class="nav-btn" id="nav-carrinho" onclick="showPage('carrinho')">Carrinho</button>
  <button class="nav-btn" id="nav-revisao" onclick="showPage('revisao')" id="nav-revisao">Revisão <span id="revisaoLock" style="display:none;opacity:.6">🔒</span></button>
</nav>

<div class="disclaimer" id="disclaimer">
  <span class="disclaimer-icon">⚠️</span>
  <span class="disclaimer-text">
    <strong>Atenção:</strong> os preços são coletados automaticamente dos sites de
    <strong>Tauste</strong>, <strong>Coop</strong> e <strong>Confiança</strong> e podem conter
    erros, estar desatualizados ou não refletir os valores praticados nas lojas.
    Este comparador é independente e não possui afiliação com nenhum dos mercados.
  </span>
  <button class="disclaimer-close" onclick="dismissDisclaimer()" title="Fechar">✕</button>
</div>

<!-- ═══════════════════════════════════════════════════════════
     DASHBOARD
     ═══════════════════════════════════════════════════════════ -->
<div class="page active" id="page-dashboard">
<main>
  <h2>Resumo Atual</h2>
  <div class="cards" id="cards"></div>

  <h2>Evolução dos Preços Médios</h2>
  <div class="charts">
    <div class="chart-box"><h3>Hortifruti</h3><canvas id="chartHorti"></canvas></div>
    <div class="chart-box"><h3>Açougue / Carnes</h3><canvas id="chartCarnes"></canvas></div>
  </div>

  <h2>Produtos (último snapshot)</h2>
  <div class="tabs">
    <button class="tab active" onclick="showTab('tauste_horti')">Tauste — Hortifruti</button>
    <button class="tab" onclick="showTab('tauste_carnes')">Tauste — Carnes</button>
    <button class="tab coop" onclick="showTab('coop_horti')">Coop — Hortifruti</button>
    <button class="tab coop" onclick="showTab('coop_acougue')">Coop — Açougue</button>
    <button class="tab confianca" onclick="showTab('confianca_horti')">Confiança — Hortifruti</button>
    <button class="tab confianca" onclick="showTab('confianca_acougue')">Confiança — Açougue</button>
  </div>
  <div class="table-box">
    <div class="search-bar"><input id="searchInput" type="text" placeholder="Buscar produto..." oninput="filterTable()"></div>
    <table><thead><tr>
      <th onclick="sortTable(0)">#</th>
      <th onclick="sortTable(1)">Produto ↕</th>
      <th onclick="sortTable(2)">Preço ↕</th>
    </tr></thead><tbody id="productBody"></tbody></table>
  </div>

  <h2>Histórico de snapshots</h2>
  <div class="table-box">
    <table class="snap-table"><thead><tr>
      <th>Data</th>
      <th>Tauste Horti</th><th>Coop Horti</th><th>Confiança Horti</th>
      <th>Tauste Carnes</th><th>Coop Açougue</th><th>Confiança Açougue</th>
    </tr></thead><tbody id="histBody"></tbody></table>
  </div>
</main>
</div>

<!-- ═══════════════════════════════════════════════════════════
     ITENS COMUNS
     ═══════════════════════════════════════════════════════════ -->
<div class="page" id="page-comuns">
<main>
  <h2>Itens Comuns entre Mercados</h2>
  <div class="search-bar">
    <input id="comunsSearch" type="text" placeholder="Buscar produto..." oninput="renderComuns()">
    <select id="comunsCat" onchange="renderComuns()">
      <option value="hortifruti">Hortifruti</option>
      <option value="acougue">Açougue</option>
    </select>
  </div>
  <div class="table-box" style="overflow-x:auto">
    <table id="comunsTable">
      <thead><tr>
        <th onclick="sortComuns('nome')">Produto ↕</th>
        <th onclick="sortComuns('tauste')" style="color:var(--tauste)">Tauste</th>
        <th onclick="sortComuns('coop')" style="color:var(--coop)">Coop</th>
        <th onclick="sortComuns('confianca')" style="color:var(--confianca)">Confiança</th>
        <th onclick="sortComuns('melhor')">Melhor R$/kg ↕</th>
        <th>Histórico</th>
      </tr></thead>
      <tbody id="comunsBody"></tbody>
    </table>
  </div>
  <p id="comunsEmpty" class="no-data" style="display:none">Nenhum item comum encontrado. Execute o comparador para gerar dados.</p>
</main>
</div>

<!-- ═══════════════════════════════════════════════════════════
     CARRINHO
     ═══════════════════════════════════════════════════════════ -->
<div class="page" id="page-carrinho">
<main>
  <h2>Monte seu Carrinho</h2>
  <div class="search-bar">
    <input id="cartSearch" type="text" placeholder="Buscar item..." oninput="renderCart()">
    <select id="cartCat" onchange="renderCart()">
      <option value="hortifruti">Hortifruti</option>
      <option value="acougue">Açougue</option>
    </select>
    <button class="btn btn-primary btn-sm" onclick="useCheapest()">★ Usar mais barato</button>
    <button class="btn btn-sm" style="background:var(--surface2);color:var(--muted)" onclick="clearCart()">Limpar</button>
  </div>
  <div class="table-box" style="overflow-x:auto">
    <div class="cart-header">
      <span></span><span>Produto</span>
      <span style="text-align:center;color:var(--tauste)">Tauste</span>
      <span style="text-align:center;color:var(--coop)">Coop</span>
      <span style="text-align:center;color:var(--confianca)">Confiança</span>
    </div>
    <div id="cartBody" class="cart-grid"></div>
    <div id="cartTotal" class="cart-total" style="display:none">
      <span>Total selecionados</span>
      <span id="totalTauste" style="text-align:center;color:var(--tauste)">—</span>
      <span id="totalCoop" style="text-align:center;color:var(--coop)">—</span>
      <span id="totalConfianca" style="text-align:center;color:var(--confianca)">—</span>
    </div>
  </div>
</main>
</div>

<!-- ═══════════════════════════════════════════════════════════
     REVISÃO
     ═══════════════════════════════════════════════════════════ -->
<div class="page" id="page-revisao">
<main>
  <h2>Pares Pendentes de Revisão</h2>
  <p style="color:var(--muted);font-size:.85rem;margin-bottom:1rem">
    Estes pares foram identificados pelo matcher mas precisam de confirmação manual.
    Decisões são salvas localmente e aplicadas na próxima coleta.
  </p>
  <div class="search-bar">
    <select id="revisaoCat" onchange="renderRevisao()">
      <option value="hortifruti">Hortifruti</option>
      <option value="acougue">Açougue</option>
    </select>
    <button class="btn btn-sm" style="background:var(--surface2);color:var(--muted)" onclick="clearDecisions()">Limpar decisões</button>
    <span id="revisaoCount" style="color:var(--muted);font-size:.82rem;margin-left:.5rem"></span>
  </div>
  <div class="table-box" style="overflow-x:auto">
    <table><thead><tr>
      <th>Score</th>
      <th style="color:var(--tauste)">Produto A</th><th>Mercado A</th><th>Preço A</th>
      <th style="color:var(--coop)">Produto B</th><th>Mercado B</th><th>Preço B</th>
      <th>Ação</th>
    </tr></thead><tbody id="revisaoBody"></tbody></table>
  </div>
</main>
</div>

<!-- Modal PIN -->
<div class="modal-overlay" id="pinOverlay">
  <div class="modal" style="max-width:320px;text-align:center">
    <div style="font-size:2rem;margin-bottom:.75rem">🔒</div>
    <div class="modal-title" style="margin-bottom:.5rem">Área restrita</div>
    <p style="color:var(--muted);font-size:.83rem;margin-bottom:1.25rem">Digite o PIN para acessar a Revisão.</p>
    <input id="pinInput" type="password" inputmode="numeric" maxlength="12"
           placeholder="PIN"
           style="width:100%;background:var(--bg);border:1px solid var(--border);border-radius:.4rem;color:var(--text);padding:.55rem .75rem;font-size:1.1rem;outline:none;text-align:center;letter-spacing:.25em"
           onkeydown="if(event.key==='Enter')submitPin()">
    <p id="pinError" style="color:var(--red);font-size:.8rem;margin-top:.5rem;min-height:1.1em"></p>
    <button class="btn btn-primary" style="width:100%;margin-top:.25rem;padding:.55rem" onclick="submitPin()">Entrar</button>
  </div>
</div>

<!-- ═══════════════════════════════════════════════════════════
     MODAL — Histórico do produto
     ═══════════════════════════════════════════════════════════ -->
<div class="modal-overlay" id="modalOverlay" onclick="closeModal(event)">
  <div class="modal">
    <div class="modal-header">
      <div class="modal-title" id="modalTitle"></div>
      <button class="modal-close" onclick="closeModal()">✕</button>
    </div>
    <canvas id="modalChart" style="margin-bottom:1.25rem"></canvas>
    <h3 style="font-size:.8rem;color:var(--muted);margin-bottom:.75rem">PREÇOS POR DIA DA SEMANA</h3>
    <div id="modalDow" style="overflow-x:auto"></div>
    <h3 style="font-size:.8rem;color:var(--muted);margin:.75rem 0 .5rem">HISTÓRICO COMPLETO</h3>
    <div id="modalHistory" style="overflow-x:auto"></div>
  </div>
</div>

<script>
const HISTORY = {data_json};
const CANONICAL = {canonical_json};
const REVISAO_PIN = {pin_js};

const MKT_COLOR = {{Tauste:'rgb(59,130,246)',Coop:'rgb(16,185,129)',Confianca:'rgb(245,158,11)'}};
const MKT_FADED = {{Tauste:'rgba(59,130,246,.12)',Coop:'rgba(16,185,129,.12)',Confianca:'rgba(245,158,11,.12)'}};
const DOW = ['Dom','Seg','Ter','Qua','Qui','Sex','Sáb'];

function fmtDate(ts) {{
  const d = new Date(ts);
  return d.toLocaleDateString('pt-BR') + ' ' + d.toLocaleTimeString('pt-BR',{{hour:'2-digit',minute:'2-digit'}});
}}
function fmtBRL(v) {{ return v != null ? 'R$ ' + (+v).toFixed(2).replace('.',',') : '—'; }}
function fmtKg(v) {{ return v != null ? 'R$ '+Number(v).toFixed(2).replace('.',',')+'/kg' : ''; }}

const latest = HISTORY.length ? HISTORY[HISTORY.length-1] : null;

document.getElementById('subtitle').textContent = latest
  ? 'Último snapshot: ' + fmtDate(latest.ts) + ' — ' + HISTORY.length + ' coleta(s)'
  : 'Nenhum dado disponível';

// ── PIN / REVISÃO AUTH ───────────────────────────────────────
const PIN_KEY = 'mkt_revisao_auth';
function isRevisaoAuth() {{
  if (!REVISAO_PIN) return true;
  try {{ return localStorage.getItem(PIN_KEY) === REVISAO_PIN; }} catch {{ return false; }}
}}
function submitPin() {{
  const val = document.getElementById('pinInput').value;
  if (val === REVISAO_PIN) {{
    try {{ localStorage.setItem(PIN_KEY, REVISAO_PIN); }} catch {{}}
    document.getElementById('pinOverlay').classList.remove('open');
    _showPage('revisao');
  }} else {{
    const err = document.getElementById('pinError');
    err.textContent = 'PIN incorreto';
    document.getElementById('pinInput').select();
    setTimeout(() => {{ err.textContent = ''; }}, 2000);
  }}
}}
(function initPin() {{
  if (REVISAO_PIN) {{
    const lock = document.getElementById('revisaoLock');
    if (lock) lock.style.display = '';
  }}
}})();

// ── PAGE NAVIGATION ──────────────────────────────────────────
function _showPage(name) {{
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('page-'+name).classList.add('active');
  document.getElementById('nav-'+name).classList.add('active');
  if (name==='comuns') renderComuns();
  if (name==='carrinho') renderCart();
  if (name==='revisao') renderRevisao();
}}
function showPage(name) {{
  if (name === 'revisao' && !isRevisaoAuth()) {{
    document.getElementById('pinOverlay').classList.add('open');
    setTimeout(() => document.getElementById('pinInput').focus(), 50);
    return;
  }}
  _showPage(name);
}}

// ── DASHBOARD — CARDS ────────────────────────────────────────
(function renderCards() {{
  if (!latest) return;
  const s = latest.stats;
  const pairs = [
    ['tauste_horti','tauste','Tauste','Hortifruti'],
    ['coop_horti','coop','Coop','Hortifruti'],
    ['confianca_horti','confianca','Confiança','Hortifruti'],
    ['tauste_carnes','tauste','Tauste','Carnes e Aves'],
    ['coop_acougue','coop','Coop','Açougue'],
    ['confianca_acougue','confianca','Confiança','Açougue'],
  ];
  const hK = ['tauste_horti','coop_horti','confianca_horti'].filter(k=>s[k]?.mean>0);
  const cK = ['tauste_carnes','coop_acougue','confianca_acougue'].filter(k=>s[k]?.mean>0);
  const wH = hK.reduce((a,b)=>s[a].mean<s[b].mean?a:b, hK[0]);
  const wC = cK.reduce((a,b)=>s[a].mean<s[b].mean?a:b, cK[0]);
  const wins = new Set([wH,wC]);
  const c = document.getElementById('cards');
  pairs.forEach(([k,cls,mkt,cat])=>{{
    const d=s[k]; if(!d||!d.count) return;
    const w=wins.has(k);
    const mn=cls==='tauste'?'Tauste':cls==='coop'?'Coop':'Confiança';
    c.innerHTML+=`<div class="card">
      <div class="card-label"><span class="badge badge-${{cls}}">${{mn}}</span> ${{cat}}${{w?'<span class="winner-tag">★ mais barato</span>':''}}</div>
      <div class="card-value">${{fmtBRL(d.mean)}}</div>
      <div class="card-sub">mediana ${{fmtBRL(d.median)}} · ${{d.count}} produtos</div>
    </div>`;
  }});
}})();

// ── DASHBOARD — CHARTS ───────────────────────────────────────
function makeChart(id, sets) {{
  const labels = HISTORY.map(h=>fmtDate(h.ts));
  new Chart(document.getElementById(id).getContext('2d'),{{
    type:'line',
    data:{{labels,datasets:sets.map(([lbl,col,key])=>({{
      label:lbl, data:HISTORY.map(h=>h.stats[key]?.mean||null),
      borderColor:col, backgroundColor:col.replace(')',',0.12)').replace('rgb','rgba'),
      tension:.3, pointRadius:4, pointHoverRadius:6, fill:true, spanGaps:true
    }}))}},
    options:{{
      responsive:true, interaction:{{mode:'index',intersect:false}},
      plugins:{{
        legend:{{labels:{{color:'#94a3b8',font:{{size:11}}}}}},
        tooltip:{{callbacks:{{label:c=>' '+c.dataset.label+': '+fmtBRL(c.parsed.y)}}}}
      }},
      scales:{{
        x:{{ticks:{{color:'#64748b',maxRotation:30}},grid:{{color:'#1e293b'}}}},
        y:{{ticks:{{color:'#64748b',callback:v=>'R$ '+v.toFixed(0)}},grid:{{color:'#334155'}}}}
      }}
    }}
  }});
}}
makeChart('chartHorti',[['Tauste','rgb(59,130,246)','tauste_horti'],['Coop','rgb(16,185,129)','coop_horti'],['Confiança','rgb(245,158,11)','confianca_horti']]);
makeChart('chartCarnes',[['Tauste','rgb(59,130,246)','tauste_carnes'],['Coop','rgb(16,185,129)','coop_acougue'],['Confiança','rgb(245,158,11)','confianca_acougue']]);

// ── DASHBOARD — PRODUCT TABLE ────────────────────────────────
let currentTab='tauste_horti', sortCol=2, sortAsc=true;
function showTab(key) {{
  currentTab=key;
  const keys=['tauste_horti','tauste_carnes','coop_horti','coop_acougue','confianca_horti','confianca_acougue'];
  document.querySelectorAll('.tab').forEach((t,i)=>t.classList.toggle('active',keys[i]===key));
  renderTable();
}}
function renderTable() {{ filterTable(latest?.stats[currentTab]?.products||[]); }}
function filterTable(products) {{
  if(!products) products=latest?.stats[currentTab]?.products||[];
  const q=document.getElementById('searchInput').value.toLowerCase();
  let rows=[...products].filter(p=>p.name.toLowerCase().includes(q));
  rows.sort((a,b)=>{{
    const va=sortCol===1?a.name:a.price, vb=sortCol===1?b.name:b.price;
    return sortAsc?(va>vb?1:-1):(va<vb?1:-1);
  }});
  document.getElementById('productBody').innerHTML=rows.map((p,i)=>
    `<tr><td>${{i+1}}</td><td>${{p.name}}</td><td class="price">${{fmtBRL(p.price)}}</td></tr>`
  ).join('')||'<tr><td colspan="3" class="no-data">Nenhum produto encontrado</td></tr>';
}}
function sortTable(col) {{
  if(sortCol===col) sortAsc=!sortAsc; else {{sortCol=col;sortAsc=true;}}
  filterTable();
}}
renderTable();

// ── DASHBOARD — HISTORY TABLE ────────────────────────────────
(function renderHistory() {{
  const body=document.getElementById('histBody');
  function cell(s,key,w) {{
    const d=s[key]; if(!d||!d.mean) return '<td>—</td>';
    const yw=key===w; const col=yw?(key.startsWith('tauste')?'#3b82f6':key.startsWith('coop')?'#10b981':'#f59e0b'):'#94a3b8';
    return `<td style="color:${{col}};font-weight:${{yw?700:400}}">${{fmtBRL(d.mean)}}</td>`;
  }}
  [...HISTORY].reverse().forEach(snap=>{{
    const s=snap.stats;
    const hK=['tauste_horti','coop_horti','confianca_horti'].filter(k=>s[k]?.mean>0);
    const cK=['tauste_carnes','coop_acougue','confianca_acougue'].filter(k=>s[k]?.mean>0);
    const wh=hK.reduce((a,b)=>s[a].mean<s[b].mean?a:b,hK[0]);
    const wc=cK.reduce((a,b)=>s[a].mean<s[b].mean?a:b,cK[0]);
    body.innerHTML+=`<tr><td>${{fmtDate(snap.ts)}}</td>${{cell(s,'tauste_horti',wh)}}${{cell(s,'coop_horti',wh)}}${{cell(s,'confianca_horti',wh)}}${{cell(s,'tauste_carnes',wc)}}${{cell(s,'coop_acougue',wc)}}${{cell(s,'confianca_acougue',wc)}}</tr>`;
  }});
}})();

// ── ITENS COMUNS ─────────────────────────────────────────────
let comunsSortKey='nome', comunsSortAsc=true;
function sortComuns(key) {{
  if(comunsSortKey===key) comunsSortAsc=!comunsSortAsc; else {{comunsSortKey=key;comunsSortAsc=true;}}
  renderComuns();
}}

function getComunsItems() {{
  const cat=document.getElementById('comunsCat').value;
  return CANONICAL?.categorias?.[cat]?.aprovados||[];
}}

function renderComuns() {{
  const q=(document.getElementById('comunsSearch').value||'').toLowerCase();
  let items=getComunsItems().filter(it=>it.nome.toLowerCase().includes(q));
  const empty=document.getElementById('comunsEmpty');
  if(!items.length){{empty.style.display='';document.getElementById('comunsBody').innerHTML='';return;}}
  empty.style.display='none';

  items.sort((a,b)=>{{
    let va,vb;
    if(comunsSortKey==='nome'){{ va=a.nome;vb=b.nome; }}
    else {{
      const mkt=comunsSortKey==='tauste'?'Tauste':comunsSortKey==='coop'?'Coop':'Confianca';
      const key=comunsSortKey==='melhor'?null:mkt;
      if(key===null){{
        va=Math.min(...Object.values(a.mercados).map(m=>m.por_kg||m.preco_atual||999));
        vb=Math.min(...Object.values(b.mercados).map(m=>m.por_kg||m.preco_atual||999));
      }} else {{
        va=a.mercados[mkt]?.preco_atual??Infinity;
        vb=b.mercados[mkt]?.preco_atual??Infinity;
      }}
    }}
    return comunsSortAsc?(va>vb?1:-1):(va<vb?1:-1);
  }});

  const mkts=['Tauste','Coop','Confianca'];
  const body=document.getElementById('comunsBody');
  body.innerHTML=items.map(it=>{{
    // Encontra melhor por_kg (ou preco_atual se por_kg indisponível)
    const vals=Object.entries(it.mercados).map(([m,d])=>{{
      return {{mkt:m,pk:d.por_kg!=null?d.por_kg:d.preco_atual,price:d.preco_atual}};
    }});
    const bestPk=Math.min(...vals.map(v=>v.pk));
    const bestMkts=new Set(vals.filter(v=>v.pk===bestPk).map(v=>v.mkt));

    function mktCell(mkt) {{
      const d=it.mercados[mkt];
      if(!d) return '<td class="muted">—</td>';
      const best=bestMkts.has(mkt);
      const pkTxt=d.por_kg!=null?`<span class="por-kg">${{fmtKg(d.por_kg)}}</span>`:'';
      return `<td${{best?' class="best"':''}}>${{fmtBRL(d.preco_atual)}}${{pkTxt}}</td>`;
    }}

    const bestMktName=[...bestMkts].map(m=>m==='Confianca'?'Confiança':m).join(', ');
    const bestPkFmt=bestPk<900?fmtKg(bestPk):'—';
    return `<tr class="clickable" onclick="openModal('${{encodeURIComponent(JSON.stringify(it))}}')">
      <td>${{it.nome}}</td>
      ${{mktCell('Tauste')}}${{mktCell('Coop')}}${{mktCell('Confianca')}}
      <td class="best">${{bestMktName}}<span class="por-kg">${{bestPkFmt}}</span></td>
      <td style="color:var(--muted);font-size:.75rem">${{it.historico?.length||0}}x</td>
    </tr>`;
  }}).join('');
}}

// ── MODAL ────────────────────────────────────────────────────
let modalChartInst=null;
function openModal(encoded) {{
  const item=JSON.parse(decodeURIComponent(encoded));
  document.getElementById('modalTitle').textContent=item.nome;
  document.getElementById('modalOverlay').classList.add('open');

  // Chart
  if(modalChartInst){{ modalChartInst.destroy(); modalChartInst=null; }}
  const hist=item.historico||[];
  if(hist.length>0) {{
    const labels=hist.map(h=>fmtDate(h.ts));
    const mkts=Object.keys(item.mercados);
    modalChartInst=new Chart(document.getElementById('modalChart').getContext('2d'),{{
      type:'line',
      data:{{labels,datasets:mkts.map(m=>{{
        const col=MKT_COLOR[m]||'#aaa';
        return {{
          label:m==='Confianca'?'Confiança':m,
          data:hist.map(h=>h.precos?.[m]??null),
          borderColor:col, backgroundColor:MKT_FADED[m]||'transparent',
          tension:.3, pointRadius:3, fill:true, spanGaps:true
        }};
      }})}},
      options:{{
        responsive:true, plugins:{{
          legend:{{labels:{{color:'#94a3b8'}}}},
          tooltip:{{callbacks:{{label:c=>' '+c.dataset.label+': '+fmtBRL(c.parsed.y)}}}}
        }},
        scales:{{
          x:{{ticks:{{color:'#64748b',maxRotation:40}},grid:{{color:'#1e293b'}}}},
          y:{{ticks:{{color:'#64748b',callback:v=>'R$ '+v.toFixed(2)}},grid:{{color:'#334155'}}}}
        }}
      }}
    }});
  }} else {{
    const ctx=document.getElementById('modalChart');
    ctx.style.display='none';
  }}

  // Day-of-week analysis
  const dowDiv=document.getElementById('modalDow');
  const mkts2=Object.keys(item.mercados);
  if(hist.length>=3) {{
    const byDow={{}}; // dow → mkt → [prices]
    hist.forEach(h=>{{
      const dow=new Date(h.ts).getDay();
      byDow[dow]=byDow[dow]||{{}};
      mkts2.forEach(m=>{{
        const p=h.precos?.[m]; if(p==null) return;
        byDow[dow][m]=byDow[dow][m]||[]; byDow[dow][m].push(p);
      }});
    }});
    const activeDows=Object.keys(byDow).map(Number).sort();
    if(activeDows.length>0) {{
      let tbl=`<table style="font-size:.78rem"><thead><tr><th>Dia</th>${{mkts2.map(m=>`<th style="color:${{MKT_COLOR[m]||'#aaa'}}">${{m==='Confianca'?'Confiança':m}}</th>`).join('')}}</tr></thead><tbody>`;
      activeDows.forEach(d=>{{
        tbl+=`<tr><td>${{DOW[d]}}</td>`;
        mkts2.forEach(m=>{{
          const ps=byDow[d]?.[m];
          tbl+=ps?`<td>${{fmtBRL(ps.reduce((a,b)=>a+b,0)/ps.length)}}</td>`:'<td style="color:var(--muted)">—</td>';
        }});
        tbl+='</tr>';
      }});
      dowDiv.innerHTML=tbl+'</tbody></table>';
    }} else dowDiv.innerHTML='<p class="no-data">Dados insuficientes</p>';
  }} else dowDiv.innerHTML='<p style="color:var(--muted);font-size:.8rem">Precisamos de mais coletas para análise por dia da semana.</p>';

  // Full history table
  const histDiv=document.getElementById('modalHistory');
  if(hist.length>0) {{
    let tbl=`<table style="font-size:.78rem"><thead><tr><th>Data</th>${{mkts2.map(m=>`<th style="color:${{MKT_COLOR[m]||'#aaa'}}">${{m==='Confianca'?'Confiança':m}}</th>`).join('')}}</tr></thead><tbody>`;
    [...hist].reverse().forEach(h=>{{
      tbl+=`<tr><td>${{fmtDate(h.ts)}}</td>${{mkts2.map(m=>{{
        const p=h.precos?.[m]; const pk=h.por_kg?.[m];
        return p!=null?`<td>${{fmtBRL(p)}}${{pk?`<span class="por-kg">${{fmtKg(pk)}}</span>`:''}}</td>`:'<td style="color:var(--muted)">—</td>';
      }}).join('')}}</tr>`;
    }});
    histDiv.innerHTML=tbl+'</tbody></table>';
  }} else histDiv.innerHTML='<p class="no-data">Sem histórico ainda.</p>';

  document.getElementById('modalChart').style.display='';
}}

function closeModal(e) {{
  if(e && e.target!==document.getElementById('modalOverlay')) return;
  document.getElementById('modalOverlay').classList.remove('open');
}}

// ── CARRINHO ─────────────────────────────────────────────────
const cartSelections={{}};  // itemId → selected market or null
const cartChecked={{}};     // itemId → bool

function getCartItems() {{
  const cat=document.getElementById('cartCat').value;
  const q=(document.getElementById('cartSearch').value||'').toLowerCase();
  return (CANONICAL?.categorias?.[cat]?.aprovados||[]).filter(it=>it.nome.toLowerCase().includes(q));
}}

function renderCart() {{
  const items=getCartItems();
  const body=document.getElementById('cartBody');
  body.innerHTML=items.map(it=>{{
    const chk=cartChecked[it.id]||false;
    const sel=cartSelections[it.id]||null;
    const mkts=['Tauste','Coop','Confianca'];
    const cols=mkts.map(m=>{{
      const d=it.mercados[m];
      if(!d) return `<div class="radio-mkt"><span class="mkt-price unavail">—</span></div>`;
      const checked=sel===m?'checked':'';
      const label=m==='Confianca'?'Confiança':m;
      const pkStr=d.por_kg!=null?`<br><span class="por-kg" style="font-size:.68rem">${{fmtKg(d.por_kg)}}</span>`:'';
      return `<div class="radio-mkt">
        <span class="mkt-price">${{fmtBRL(d.preco_atual)}}${{pkStr}}</span>
        <label><input type="radio" name="mkt_${{it.id}}" value="${{m}}" ${{checked}} onchange="setMkt('${{it.id}}','${{m}}')"> ${{label}}</label>
      </div>`;
    }});
    return `<div class="cart-row">
      <input type="checkbox" ${{chk?'checked':''}} onchange="setCheck('${{it.id}}',this.checked)">
      <span class="cart-row-name">${{it.nome}}</span>
      <div class="cart-row-mkts">${{cols.join('')}}</div>
    </div>`;
  }}).join('')||'<div class="no-data">Nenhum item encontrado</div>';
  updateCartTotals();
}}

function setCheck(id,v){{ cartChecked[id]=v; updateCartTotals(); }}
function setMkt(id,m){{ cartSelections[id]=m; updateCartTotals(); }}

function updateCartTotals() {{
  const items=getCartItems().filter(it=>cartChecked[it.id]);
  const totals={{}};
  items.forEach(it=>{{
    const m=cartSelections[it.id]; if(!m) return;
    const d=it.mercados[m]; if(!d) return;
    totals[m]=(totals[m]||0)+d.preco_atual;
  }});
  const tot=document.getElementById('cartTotal');
  if(!items.length){{ tot.style.display='none'; return; }}
  tot.style.display='grid';
  document.getElementById('totalTauste').textContent=totals['Tauste']!=null?fmtBRL(totals['Tauste']):'—';
  document.getElementById('totalCoop').textContent=totals['Coop']!=null?fmtBRL(totals['Coop']):'—';
  document.getElementById('totalConfianca').textContent=totals['Confianca']!=null?fmtBRL(totals['Confianca']):'—';
}}

function useCheapest() {{
  getCartItems().forEach(it=>{{
    cartChecked[it.id]=true;
    let best=null,bestP=Infinity;
    ['Tauste','Coop','Confianca'].forEach(m=>{{
      const d=it.mercados[m]; if(!d) return;
      const p=d.por_kg!=null?d.por_kg:d.preco_atual;
      if(p<bestP){{ bestP=p; best=m; }}
    }});
    if(best) cartSelections[it.id]=best;
  }});
  renderCart();
}}

function clearCart() {{
  getCartItems().forEach(it=>{{ delete cartChecked[it.id]; delete cartSelections[it.id]; }});
  renderCart();
}}

// ── REVISÃO ──────────────────────────────────────────────────
const DECISIONS_KEY='mkt_decisions_v1';
function loadDecisions(){{ try{{return JSON.parse(localStorage.getItem(DECISIONS_KEY)||'{{}}')||{{}};}}catch{{return{{}};}} }}
function saveDecisions(d){{ localStorage.setItem(DECISIONS_KEY,JSON.stringify(d)); }}
function clearDecisions(){{ localStorage.removeItem(DECISIONS_KEY); renderRevisao(); }}

function renderRevisao() {{
  const cat=document.getElementById('revisaoCat').value;
  const pendentes=(CANONICAL?.categorias?.[cat]?.pendentes||[]);
  const decisions=loadDecisions();
  const body=document.getElementById('revisaoBody');
  document.getElementById('revisaoCount').textContent=`${{pendentes.length}} pares pendentes`;

  body.innerHTML=pendentes.map((p,i)=>{{
    const key=`${{cat}}|${{p.mercado_a}}:${{p.nome_a}}|${{p.mercado_b}}:${{p.nome_b}}`;
    const dec=decisions[key];
    const pct=Math.round(p.score);
    const barColor=pct>=75?'#4ade80':pct>=65?'#fbbf24':'#f87171';
    const scoreBadge=`${{pct}}% <span class="score-bar" style="width:${{pct*.5}}px;background:${{barColor}}"></span>`;
    const btns=dec?
      `<td><span style="color:${{dec==='approve'?'var(--green)':'var(--red)'}};font-size:.8rem">${{dec==='approve'?'✓ Aprovado':'✗ Rejeitado'}}</span>
       <button class="btn btn-sm" style="margin-left:.4rem;background:var(--surface2);color:var(--muted)" onclick="decide('${{key}}',null)">Desfazer</button></td>`:
      `<td><button class="btn btn-sm btn-green" onclick="decide('${{key}}','approve')">✓ Aprovar</button>
       <button class="btn btn-sm btn-red" style="margin-left:.3rem" onclick="decide('${{key}}','reject')">✗ Rejeitar</button></td>`;
    const mColor=n=>n==='Tauste'?'var(--tauste)':n==='Coop'?'var(--coop)':'var(--confianca)';
    return `<tr style="opacity:${{dec?0.5:1}}">
      <td>${{scoreBadge}}</td>
      <td>${{p.nome_a}}</td>
      <td style="color:${{mColor(p.mercado_a)}}">${{p.mercado_a==='Confianca'?'Confiança':p.mercado_a}}</td>
      <td class="price">${{fmtBRL(p.preco_a)}}</td>
      <td>${{p.nome_b}}</td>
      <td style="color:${{mColor(p.mercado_b)}}">${{p.mercado_b==='Confianca'?'Confiança':p.mercado_b}}</td>
      <td class="price">${{fmtBRL(p.preco_b)}}</td>
      ${{btns}}
    </tr>`;
  }}).join('')||'<tr><td colspan="8" class="no-data">Nenhum par pendente nesta categoria.</td></tr>';
}}

function decide(key,val) {{
  const d=loadDecisions();
  if(val===null) delete d[key]; else d[key]=val;
  saveDecisions(d);
  renderRevisao();
}}

// ── DISCLAIMER ───────────────────────────────────────────────
function dismissDisclaimer() {{
  document.getElementById('disclaimer').classList.add('hidden');
  try{{ localStorage.setItem('disclaimer_dismissed','1'); }}catch{{}}
}}
(function(){{
  try{{ if(localStorage.getItem('disclaimer_dismissed')==='1') dismissDisclaimer(); }}catch{{}}
}})();
</script>

<footer>
  Dados coletados automaticamente dos sites
  <a href="https://tauste.com.br" target="_blank" rel="noopener">Tauste</a>,
  <a href="https://www.coopsupermercado.com.br" target="_blank" rel="noopener">Coop</a> e
  <a href="https://www.confianca.com.br" target="_blank" rel="noopener">Confiança</a>.<br>
  Os preços podem conter erros e não refletem necessariamente os valores praticados nas lojas.<br>
  Este comparador é independente e não possui afiliação com nenhum dos mercados.
</footer>
</body>
</html>"""

    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Visualizador gerado: {HTML_FILE}")


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

def main() -> None:

    print("=" * 64)
    print("  Comparador de Mercados — Sorocaba")
    print("=" * 64)

    collected: dict[str, list[dict]] = {}

    # ── Tauste ──────────────────────────────────────────────
    print("\n[1/6] Hortifruti — Tauste")
    collected["tauste_horti"] = scrape_tauste(
        "https://tauste.com.br/sorocaba3/hortifruti.html",
        label="Hortifruti",
        max_pages=5,
    )

    print("\n[2/6] Carnes e Aves — Tauste")
    collected["tauste_carnes"] = scrape_tauste(
        "https://tauste.com.br/sorocaba3/carnes-e-aves.html",
        label="Carnes e Aves",
        max_pages=7,
    )

    # ── Coop ────────────────────────────────────────────────
    print("\n[3/6] Hortifruti — Coop")
    collected["coop_horti"] = scrape_coop(
        category_url="https://www.coopsupermercado.com.br/hortifruti",
        category_slug="hortifruti",
        label="Hortifruti",
    )

    print("\n[4/6] Açougue — Coop")
    collected["coop_acougue"] = scrape_coop(
        category_url="https://www.coopsupermercado.com.br/acougue",
        category_slug="acougue",
        label="Açougue",
    )

    # ── Confiança ───────────────────────────────────────────
    print("\n[5/6] Hortifruti — Confiança")
    collected["confianca_horti"] = scrape_confianca(
        "https://www.confianca.com.br/sorocaba/c/hortifruti/s_hortifruti?Ns=product.analytics.factorQuantitySold30d|1",
        label="Hortifruti",
    )

    print("\n[6/6] Açougue — Confiança")
    collected["confianca_acougue"] = scrape_confianca(
        "https://www.confianca.com.br/sorocaba/c/acougue/s_acougue?Ns=product.analytics.factorQuantitySold30d|1",
        label="Açougue",
    )

    # ── Relatório no terminal ────────────────────────────────
    results = [
        (
            "Hortifruti",
            analyze(collected["tauste_horti"], "Tauste", "Hortifruti"),
            analyze(collected["coop_horti"], "Coop", "Hortifruti"),
            analyze(collected["confianca_horti"], "Confianca", "Hortifruti"),
        ),
        (
            "Açougue / Carnes",
            analyze(collected["tauste_carnes"], "Tauste", "Carnes"),
            analyze(collected["coop_acougue"], "Coop", "Açougue"),
            analyze(collected["confianca_acougue"], "Confianca", "Açougue"),
        ),
    ]
    print_report(results)

    # ── Histórico e HTML ────────────────────────────────────
    ts = datetime.now().isoformat()
    history = load_history()
    snap = snapshot_summary(collected, ts)
    history.append(snap)
    save_history(history)

    # ── Matcher de produtos comuns ───────────────────────────
    canonical = None
    try:
        import matcher
        canonical = matcher.run(snap["stats"], ts)
    except ImportError:
        print("matcher.py não encontrado — itens comuns não serão calculados.")
    except Exception as exc:
        print(f"Matcher falhou: {exc}")

    revisao_pin = os.environ.get("REVISAO_PIN", "")
    generate_html(history, canonical, revisao_pin=revisao_pin)
    print(f"\nAbra o visualizador: {os.path.abspath(HTML_FILE)}")


if __name__ == "__main__":
    main()
