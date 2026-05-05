#!/usr/bin/env python3
"""
Matcher de Produtos — combina itens equivalentes entre mercados.
Usa fuzzy matching (rapidfuzz) para identificar o mesmo produto
em Tauste, Coop e Confiança, calcula R$/kg e mantém histórico por item.

Dependência: pip install rapidfuzz
"""

import os
import re
import json
import unicodedata
import sys
from collections import defaultdict
from datetime import datetime

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

try:
    from rapidfuzz import fuzz, process as rf_process
except ImportError:
    print("rapidfuzz não instalado. Execute: pip install rapidfuzz")
    raise

CANONICAL_FILE = "produtos_canonicos.json"

AUTO_ACCEPT = 85   # score >= AUTO_ACCEPT → aprovado automaticamente
REVIEW_MIN  = 55   # score >= REVIEW_MIN  → fila de revisão

CATEGORY_KEYS = {
    "hortifruti": ["tauste_horti", "coop_horti",    "confianca_horti"],
    "acougue":    ["tauste_carnes", "coop_acougue", "confianca_acougue"],
}

KEY_TO_MARKET = {
    "tauste_horti":    "Tauste",
    "tauste_carnes":   "Tauste",
    "coop_horti":      "Coop",
    "coop_acougue":    "Coop",
    "confianca_horti": "Confianca",
    "confianca_acougue": "Confianca",
}


# ─────────────────────────────────────────────────────────────
# NORMALIZAÇÃO
# ─────────────────────────────────────────────────────────────

_RE_WEIGHTS = re.compile(r"\b\d+[,.]?\d*\s*(kg|g|ml|l|gramas?|litros?)\b", re.IGNORECASE)
_RE_NOISE   = re.compile(
    r"\b(aprox|aproximadamente|sem|congelado|resfriado|organico|organica"
    r"|hidroponico|hidroponica|granel|bandeja|pote|caixa|saco|pacote"
    r"|unidade|und|un|pc|pcs|embalagem|emb|extra|especial|premium"
    r"|selecionado|tipo|nobre|fresco|frescos|fresquinho)\b",
    re.IGNORECASE,
)
_RE_NUMBERS    = re.compile(r"\b\d+\b")
_RE_MULTI_SP   = re.compile(r"\s{2,}")


def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )


def normalize(name: str) -> str:
    """Normaliza nome para matching: remove pesos, ruídos, acentos, números."""
    s = _strip_accents(name.lower())
    s = _RE_WEIGHTS.sub(" ", s)
    s = _RE_NOISE.sub(" ", s)
    s = _RE_NUMBERS.sub(" ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return _RE_MULTI_SP.sub(" ", s).strip()


def extract_kg(name: str) -> float | None:
    """Extrai peso em kg do nome do produto."""
    n = name.lower()
    m = re.search(r"(\d+[,.]?\d*)\s*kg", n)
    if m:
        return float(m.group(1).replace(",", "."))
    m = re.search(r"(\d+)\s*g\b", n)
    if m:
        return int(m.group(1)) / 1000
    return None


def price_per_kg(price: float, weight_kg: float | None) -> float | None:
    if not weight_kg or weight_kg <= 0:
        return None
    return round(price / weight_kg, 2)


def _slug(name: str) -> str:
    s = _strip_accents(name.lower())
    s = re.sub(r"[^\w\s-]", "", s)
    return re.sub(r"\s+", "-", s).strip("-")[:60]


def _shortest(names: list[str]) -> str:
    return min(names, key=len)


# ─────────────────────────────────────────────────────────────
# MATCHING — grafo de componentes conectados
# ─────────────────────────────────────────────────────────────

def _connected_components(edges: list[tuple]) -> list[frozenset]:
    """Union-Find: retorna componentes conectados a partir de lista de arestas (a, b)."""
    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]

    def union(x, y):
        parent[find(x)] = find(y)

    for a, b in edges:
        union(a, b)

    groups: dict = defaultdict(set)
    for node in parent:
        groups[find(node)].add(node)
    return [frozenset(g) for g in groups.values()]


def build_matches(snapshot_stats: dict) -> dict:
    """
    Recebe snapshot_stats (dict key→list[product]) e retorna:
      {"hortifruti": {"aprovados": [...], "pendentes": [...]},
       "acougue":    {"aprovados": [...], "pendentes": [...]}}
    """
    result: dict = {}

    for cat, keys in CATEGORY_KEYS.items():
        # Agrupa produtos por mercado para esta categoria
        # snapshot_stats[key] pode ser lista direta OU dict com "products" (formato historico.json)
        by_market: dict[str, list[dict]] = {}
        for key in keys:
            mkt = KEY_TO_MARKET[key]
            raw = snapshot_stats.get(key, [])
            by_market[mkt] = raw.get("products", []) if isinstance(raw, dict) else raw

        markets = [m for m, prods in by_market.items() if prods]
        if len(markets) < 2:
            result[cat] = {"aprovados": [], "pendentes": []}
            continue

        # Pré-computa nomes normalizados
        norms: dict[str, list[str]] = {
            m: [normalize(p["name"]) for p in by_market[m]] for m in markets
        }

        # Coleta todas as arestas auto-aprovadas e pendentes
        auto_edges: list[tuple[tuple, tuple, float]] = []  # (node_a, node_b, score)
        pend_edges: list[tuple[tuple, tuple, float]] = []

        pairs = [(markets[i], markets[j]) for i in range(len(markets)) for j in range(i+1, len(markets))]

        for mkt_a, mkt_b in pairs:
            prods_a = by_market[mkt_a]
            prods_b = by_market[mkt_b]
            norms_b = norms[mkt_b]

            for ia, prod_a in enumerate(prods_a):
                node_a = (mkt_a, prod_a["name"])
                best = rf_process.extractOne(
                    norms[mkt_a][ia],
                    norms_b,
                    scorer=fuzz.token_sort_ratio,
                    score_cutoff=REVIEW_MIN,
                )
                if not best:
                    continue
                _, score, ib = best
                node_b = (mkt_b, prods_b[ib]["name"])

                if score >= AUTO_ACCEPT:
                    auto_edges.append((node_a, node_b, score))
                else:
                    pend_edges.append((node_a, node_b, score))

        # Componentes conectados das arestas auto-aprovadas
        components = _connected_components([(a, b) for a, b, _ in auto_edges])

        # Índice de scores por par de nós
        edge_scores: dict[tuple, float] = {}
        for na, nb, s in auto_edges:
            key = (na, nb)
            edge_scores[key] = max(edge_scores.get(key, 0), s)

        # Nós já emparelhados (não entram na fila de revisão)
        matched_nodes: set = set()
        for comp in components:
            matched_nodes.update(comp)

        # Lookup rápido nome→produto
        prod_lookup: dict[tuple, dict] = {}
        for mkt, prods in by_market.items():
            for p in prods:
                prod_lookup[(mkt, p["name"])] = p

        # Monta itens canônicos a partir dos componentes
        aprovados: list[dict] = []
        for comp in components:
            by_mkt_in_comp: dict[str, list[str]] = defaultdict(list)
            for mkt, name in comp:
                by_mkt_in_comp[mkt].append(name)

            # Calcula confiança média das arestas no componente
            comp_list = list(comp)
            edge_count, score_sum = 0, 0.0
            for i in range(len(comp_list)):
                for j in range(i+1, len(comp_list)):
                    s = edge_scores.get((comp_list[i], comp_list[j]),
                        edge_scores.get((comp_list[j], comp_list[i]), 0))
                    if s:
                        score_sum += s
                        edge_count += 1
            avg_conf = round(score_sum / edge_count / 100, 3) if edge_count else 0.0

            # Monta dict de mercados
            item_markets: dict = {}
            canon_names: list[str] = []
            for mkt, names in by_mkt_in_comp.items():
                name = _shortest(names)
                canon_names.append(name)
                prod = prod_lookup.get((mkt, name))
                if prod:
                    kg = extract_kg(name)
                    item_markets[mkt] = {
                        "nome": name,
                        "peso_kg": kg,
                        "preco_atual": prod["price"],
                        "por_kg": price_per_kg(prod["price"], kg),
                    }

            if len(item_markets) < 2:
                continue  # solitário — sem comparação possível

            canon_name = _shortest(canon_names)
            aprovados.append({
                "id": _slug(canon_name),
                "nome": canon_name,
                "mercados": item_markets,
                "confidence": avg_conf,
                "historico": [],
            })

        # Fila de revisão: apenas pares cujos dois nós não estão emparelhados
        seen_pairs: set = set()
        pendentes: list[dict] = []
        for na, nb, score in pend_edges:
            if na in matched_nodes or nb in matched_nodes:
                continue
            pair_key = tuple(sorted([f"{na[0]}:{na[1]}", f"{nb[0]}:{nb[1]}"]))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            pa = prod_lookup.get(na)
            pb = prod_lookup.get(nb)
            if pa and pb:
                pendentes.append({
                    "nome_a": na[1], "mercado_a": na[0], "preco_a": pa["price"],
                    "nome_b": nb[1], "mercado_b": nb[0], "preco_b": pb["price"],
                    "score": round(score, 1),
                })

        pendentes.sort(key=lambda x: x["score"], reverse=True)

        result[cat] = {
            "aprovados": sorted(aprovados, key=lambda x: x["nome"].lower()),
            "pendentes": pendentes[:300],
        }

        print(f"  {cat}: {len(aprovados)} matches automáticos, {len(pendentes)} para revisão")

    return result


# ─────────────────────────────────────────────────────────────
# PERSISTÊNCIA
# ─────────────────────────────────────────────────────────────

def load_canonical() -> dict:
    if not os.path.exists(CANONICAL_FILE):
        return {"atualizado_em": None, "categorias": {}}
    try:
        with open(CANONICAL_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, KeyError):
        return {"atualizado_em": None, "categorias": {}}


def save_canonical(data: dict) -> None:
    with open(CANONICAL_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    total_ap = sum(len(c.get("aprovados", [])) for c in data.get("categorias", {}).values())
    total_pe = sum(len(c.get("pendentes", [])) for c in data.get("categorias", {}).values())
    print(f"  Canônicos salvos em {CANONICAL_FILE}: {total_ap} aprovados, {total_pe} pendentes")


def _merge_aprovados(existing: list[dict], new_list: list[dict]) -> list[dict]:
    """
    Mescla aprovados novos com existentes.
    Preserva histórico e adições manuais; atualiza preços correntes.
    """
    by_id: dict[str, dict] = {item["id"]: item for item in existing}

    for new in new_list:
        if new["id"] in by_id:
            ex = by_id[new["id"]]
            for mkt, info in new["mercados"].items():
                ex["mercados"][mkt] = info
            ex["confidence"] = new["confidence"]
        else:
            by_id[new["id"]] = new

    return sorted(by_id.values(), key=lambda x: x["nome"].lower())


def update_history(canonical: dict, ts: str) -> None:
    """Anexa snapshot de preços ao histórico de cada canônico aprovado."""
    for cat_data in canonical.get("categorias", {}).values():
        for item in cat_data.get("aprovados", []):
            precos = {mkt: info["preco_atual"] for mkt, info in item["mercados"].items()}
            por_kg = {mkt: info["por_kg"] for mkt, info in item["mercados"].items()
                      if info.get("por_kg") is not None}
            item.setdefault("historico", []).append({
                "ts": ts,
                "precos": precos,
                "por_kg": por_kg if por_kg else None,
            })


# ─────────────────────────────────────────────────────────────
# PIPELINE PRINCIPAL
# ─────────────────────────────────────────────────────────────

def run(latest_stats: dict, ts: str) -> dict:
    """
    Pipeline completo:
    1. Fuzzy-match produtos do snapshot mais recente
    2. Mescla com canônicos existentes (preserva histórico)
    3. Adiciona ponto de histórico com preços atuais
    4. Salva produtos_canonicos.json e retorna o canonical atualizado
    """
    print("\nMatcher: cruzando produtos entre mercados...")

    new_matches = build_matches(latest_stats)

    canonical = load_canonical()
    canonical["atualizado_em"] = ts

    for cat, data in new_matches.items():
        if cat not in canonical["categorias"]:
            canonical["categorias"][cat] = {"aprovados": [], "pendentes": []}

        canonical["categorias"][cat]["aprovados"] = _merge_aprovados(
            canonical["categorias"][cat].get("aprovados", []),
            data["aprovados"],
        )
        canonical["categorias"][cat]["pendentes"] = data["pendentes"]

    update_history(canonical, ts)
    save_canonical(canonical)

    return canonical


# ─────────────────────────────────────────────────────────────
# USO STANDALONE (teste)
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    history_path = "historico.json"
    if not os.path.exists(history_path):
        print(f"Arquivo {history_path} não encontrado. Execute comparador.py primeiro.")
        sys.exit(1)

    with open(history_path, "r", encoding="utf-8") as f:
        history = json.load(f)

    snapshots = history.get("snapshots", [])
    if not snapshots:
        print("Nenhum snapshot no histórico.")
        sys.exit(1)

    latest = snapshots[-1]
    print(f"Processando snapshot de {latest['ts']}")
    canonical = run(latest["stats"], latest["ts"])

    # Mostra amostra dos matches
    for cat, data in canonical["categorias"].items():
        print(f"\n=== {cat.upper()} — top 10 matches ===")
        for item in data["aprovados"][:10]:
            mercados_str = ", ".join(
                f"{m}: R${info['preco_atual']:.2f}" + (f" (R${info['por_kg']:.2f}/kg)" if info.get("por_kg") else "")
                for m, info in item["mercados"].items()
            )
            print(f"  [{item['confidence']:.0%}] {item['nome']} → {mercados_str}")
        if data["pendentes"]:
            print(f"  ... {len(data['pendentes'])} pares aguardando revisão")
