#!/usr/bin/env python3
"""
Build a starting org tree for POST /import.

THIS REPO CARRIES NO CLIENT DATA. The generator that seeded the live instance held the real
people, roles and interview notes inline, so it stays outside version control (seed/*.json and
seed/build-seed.local.py are gitignored). What is here is the same shape with a fictional org,
so a fresh clone can be seeded and exercised end to end.

Seeding discipline the real generator follows, and any replacement should too:
  · seed ONLY what the interviews and brief establish — do not invent reporting lines
  · below the HOD layer, group people by function and attach an open note saying the line is
    unconfirmed, rather than drawing it as fact
  · set RAG only where the condition is documented:
      blue  = key-man / single point of failure
      red   = actively broken or at risk of walking
      grey  = role undefined or to-hire
    everything else stays unset — an unset box is an honest box

Output: seed/example-org.json   Push with: SEED_FILE=seed/example-org.json seed/push-seed.sh
"""
import json, pathlib

AUTHOR = "Consultant"

def U(name, title="", entity="", rag=None, tags=None, notes=None, reports=None):
    return {"type": "unit", "name": name, "title": title, "entity": entity, "rag": rag,
            "tags": tags or [], "notes": notes or [], "cost": None, "reports": reports or []}

def P(name, title, entity="", rag=None, tags=None, notes=None, reports=None):
    return {"type": "person", "name": name, "title": title, "entity": entity, "rag": rag,
            "tags": tags or [], "notes": notes or [], "cost": None, "reports": reports or []}

def n(body, owner=None, status=None):
    return {"author": AUTHOR, "body": body, "owner": owner, "status": status, "sys": False}

UNCONFIRMED = n("Reporting line below this level is not established. Grouped by function from the "
                "interviews; the team should confirm or redraw it.", status="open")

root = U("Example Co", "Fictional org for local development", "Group",
  notes=[n("Example data only. Replace with the real seed on the server, never in this repo.")],
  reports=[
    P("Owner", "Owner · Managing Director", "HO", rag="blue", tags=["key-man"],
      notes=[n("Every escalation ends here — the finding, not the design.")],
      reports=[
        P("HOD Sales", "HOD — Sales", "HO", reports=[
          U("Sales team", "Sales", "HO", notes=[UNCONFIRMED], reports=[
            P("Sales Manager", "Sales Manager", "HO"),
            P("Estimation Engineer", "Estimation Engineer", "HO"),
          ]),
        ]),
        P("HOD Projects", "HOD — Projects", "HO", reports=[
          U("Sites", "Projects", "HO", notes=[UNCONFIRMED], reports=[
            P("Site Engineer", "Site Engineer", "GOA"),
            P("Site Engineer (to hire)", "Site Engineer", "BLR", rag="grey"),
          ]),
        ]),
        P("HOD HR", "HOD — HR", "HO", reports=[
          P("HR Generalist", "HR — Generalist", "HO"),
          P("HR Recruiter", "HR — Recruitment", "HO", rag="red",
            notes=[n("Example of a documented at-risk flag.", status="open")]),
        ]),
        U("Finance", "Finance", "HO", rag="grey", notes=[UNCONFIRMED]),
      ]),
  ])

out = pathlib.Path(__file__).parent / "example-org.json"
out.write_text(json.dumps({"root": root}, indent=1, ensure_ascii=False))

def count(node, k=("unit", "person")):
    c = {"unit": 0, "person": 0, "notes": len(node.get("notes") or [])}
    c[node["type"]] += 1
    for r in node.get("reports") or []:
        s = count(r)
        for key in c: c[key] += s[key]
    return c
c = count(root)
print(f"  wrote {out.name}: {c['unit'] + c['person']} nodes "
      f"({c['person']} people · {c['unit']} units) · {c['notes']} notes")
