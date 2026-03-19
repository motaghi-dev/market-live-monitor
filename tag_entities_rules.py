import csv
import re
from pathlib import Path

from db import (
    connect, init_db,
    get_or_create_entity, add_alias, insert_chunk_entity
)

DB_PATH = Path("algoalps.db")
STOCKS_CSV = Path("stocks_universe.csv")

# -------------------------
# Canonical alias dictionaries 
# -------------------------
PERSON_ALIASES = {
    "Donald Trump": [
        "Trump", "Donald Trump", "Donald J Trump", "Donald J. Trump",
        "President Trump", "Donald John Trump"
    ],
}

COUNTRY_ALIASES = {
    "United States": ["USA", "U.S.", "US", "United States", "America"],
    "China": ["China", "PRC"],
    "Russia": ["Russia", "Russian Federation"],
    "Ukraine": ["Ukraine"],
    "United Kingdom": ["UK", "U.K.", "Britain", "United Kingdom"],
    "European Union": ["EU", "European Union"],
}

# Macro topics
MACRO_RULES = {
    "CPI": [r"\bcpi\b", r"\bconsumer price index\b"],
    "Inflation": [r"\binflation\b", r"\binflationary\b"],
    "Fed": [r"\bfed\b", r"\bfederal reserve\b", r"\bfomc\b"],
    "Rates": [r"\brate(s)?\b", r"\binterest rate(s)?\b", r"\bhike(s|d|ing)?\b", r"\bcut(s|ting)?\b"],
    "GDP": [r"\bgdp\b", r"\bgross domestic product\b"],
    "Jobs": [r"\bunemployment\b", r"\bnfp\b", r"\bpayrolls?\b", r"\bjobs?\b"],
    "Oil": [r"\bbrent\b", r"\bwti\b", r"\boil\b"],
    "Tariffs": [r"\btariff(s)?\b"],
    "Sanctions": [r"\bsanction(s)?\b"],
}

CONF = {
    "STOCK": 0.95,
    "PERSON": 0.90,
    "COUNTRY": 0.90,
    "MACRO": 0.80,
}


def load_stock_universe(csv_path: Path):
    if not csv_path.exists():
        raise RuntimeError(f"Missing {csv_path}. Create stocks_universe.csv in project root.")
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            ticker = (r.get("ticker") or "").strip().upper()
            name = (r.get("name") or "").strip()
            aliases = (r.get("aliases") or "").strip()
            alias_list = [a.strip() for a in aliases.split("|") if a.strip()] if aliases else []
            rows.append({"ticker": ticker, "name": name, "aliases": alias_list})
    return rows


def build_alias_index(conn, type_, canonical_to_aliases, meta_by_canonical=None):
    """
    Creates entities+aliases in DB and returns:
      - alias_to_entity_id: dict[str_lower] -> entity_id
      - regex: one combined regex matching any alias (case-insensitive)
    """
    alias_to_entity_id = {}
    all_aliases = []

    for canonical, aliases in canonical_to_aliases.items():
        meta = meta_by_canonical.get(canonical) if meta_by_canonical else None
        eid = get_or_create_entity(conn, canonical=canonical, type_=type_, meta=meta)

        # include canonical as alias 
        for a in [canonical] + list(aliases):
            a = (a or "").strip()
            if not a:
                continue
            add_alias(conn, eid, a)
            alias_to_entity_id[a.lower()] = eid
            all_aliases.append(a)

 
    # Sort by length so longer phrases match first
    uniq = sorted(set(all_aliases), key=len, reverse=True)
    if not uniq:
        return {}, None

    
    parts = [re.escape(x) for x in uniq]
    pattern = r"(?i)(?<!\w)(" + "|".join(parts) + r")(?!\w)"
    regex = re.compile(pattern)
    return alias_to_entity_id, regex


def bootstrap_entities(conn):
    # Stocks from universe
    stocks = load_stock_universe(STOCKS_CSV)
    stock_meta = {}
    stock_aliases = {}
    for s in stocks:
        ticker = s["ticker"]
        if not ticker:
            continue
        stock_meta[ticker] = {"name": s["name"]}
        # aliases include ticker, company name, and custom aliases
        aliases = []
        if s["name"]:
            aliases.append(s["name"])
        aliases.append(ticker)
        aliases.extend(s["aliases"])
        stock_aliases[ticker] = aliases

    stock_map, stock_regex = build_alias_index(conn, "STOCK", stock_aliases, meta_by_canonical=stock_meta)

    # People
    person_map, person_regex = build_alias_index(conn, "PERSON", PERSON_ALIASES)

    # Countries
    country_map, country_regex = build_alias_index(conn, "COUNTRY", COUNTRY_ALIASES)

    # Macros 
    macro_entities = {}
    for canonical, patterns in MACRO_RULES.items():
        eid = get_or_create_entity(conn, canonical=canonical, type_="MACRO", meta=None)
        macro_entities[canonical] = (eid, [re.compile(p, re.IGNORECASE) for p in patterns])

    return (stock_map, stock_regex), (person_map, person_regex), (country_map, country_regex), macro_entities


def tag_from_regex(conn, chunk_id, text, type_name, alias_to_entity_id, regex, confidence, source):
    if not regex:
        return
    seen = set()  
    for m in regex.finditer(text):
        mention = m.group(1)
        eid = alias_to_entity_id.get(mention.lower())
        if not eid:
            continue
        key = (eid, mention.lower())
        if key in seen:
            continue
        seen.add(key)
        insert_chunk_entity(conn, chunk_id, eid, mention, confidence, source)


def main():
    conn = connect(DB_PATH)
    init_db(conn)

    (stock_map, stock_regex), (person_map, person_regex), (country_map, country_regex), macro_entities = bootstrap_entities(conn)

    # Tag chunks that already have transcripts
    rows = conn.execute("""
        SELECT c.id AS chunk_id, t.text
        FROM chunks c
        JOIN transcripts t ON t.chunk_id = c.id
        ORDER BY c.id ASC
    """).fetchall()

    print("Tagging transcripts:", len(rows))

    for r in rows:
        chunk_id = int(r["chunk_id"])
        text = (r["text"] or "").strip()
        if not text:
            continue

        # Stocks / People / Countries
        tag_from_regex(conn, chunk_id, text, "STOCK", stock_map, stock_regex, CONF["STOCK"], "rules")
        tag_from_regex(conn, chunk_id, text, "PERSON", person_map, person_regex, CONF["PERSON"], "rules")
        tag_from_regex(conn, chunk_id, text, "COUNTRY", country_map, country_regex, CONF["COUNTRY"], "rules")

        # Macros
        t_low = text.lower()
        for canonical, (eid, pats) in macro_entities.items():
            for pat in pats:
                if pat.search(t_low):
                    insert_chunk_entity(conn, chunk_id, eid, canonical, CONF["MACRO"], "macros")
                    break

    print("Done tagging. Check chunk_entities in the DB.")


if __name__ == "__main__":
    main()
