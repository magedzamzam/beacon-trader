import re, sqlite3, json, glob, os

DUMP = sorted(glob.glob(r"C:/Users/opc/Documents/GitHub/beacon-data-dump/beacon_*.sql"))[-1]

def _split_tuples(body):
    out, buf, depth, q = [], "", 0, False
    i = 0
    while i < len(body):
        c = body[i]
        if c == "'" :
            # count consecutive quotes for escaping
            buf += c; q = not q; i += 1; continue
        if not q:
            if c == '(':
                if depth == 0:
                    buf = ""
                    depth += 1; i += 1; continue
                depth += 1
            elif c == ')':
                depth -= 1
                if depth == 0:
                    out.append(buf); i += 1; continue
        buf += c; i += 1
    return out

def _parse_vals(t):
    vals, buf, q, i = [], "", False, 0
    while i < len(t):
        c = t[i]
        if c == "'":
            if q and i+1 < len(t) and t[i+1] == "'":
                buf += "'"; i += 2; continue
            q = not q; i += 1; continue
        if c == ',' and not q:
            vals.append(buf); buf = ""; i += 1; continue
        buf += c; i += 1
    vals.append(buf)
    res = []
    for v in vals:
        v = v.strip()
        if v == 'NULL': res.append(None)
        else: res.append(v)
    return res

def load(path=DUMP):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    txt = open(path, encoding="utf-8").read()
    # join multiline INSERTs: gather statements ending in ';'
    stmts = re.findall(r"INSERT INTO public\.(\w+)\s*\(([^)]*)\)\s*VALUES\s*(.*?);\n", txt, re.S)
    tables = {}
    for tname, cols, body in stmts:
        cols = [c.strip().strip('"') for c in cols.split(',')]
        tables.setdefault(tname, cols)
        tuples = _split_tuples(body)
        rows = [_parse_vals(t) for t in tuples]
        tables[tname+"__rows"] = tables.get(tname+"__rows", []) + rows
    for tname in [k for k in tables if not k.endswith("__rows")]:
        cols = tables[tname]
        con.execute(f"CREATE TABLE {tname} ({','.join('\"'+c+'\"' for c in cols)})")
        rows = tables[tname+"__rows"]
        good = [r for r in rows if len(r) == len(cols)]
        con.executemany(f"INSERT INTO {tname} VALUES ({','.join('?'*len(cols))})", good)
    con.commit()
    return con
