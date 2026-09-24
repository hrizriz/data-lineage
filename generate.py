"""Generate index.html from catalog.yaml.

Run after the contract graph changes:

    python generate.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML required: pip install pyyaml") from exc

HERE = Path(__file__).resolve().parent
CATALOG_PATH = HERE / "catalog.yaml"
HTML_PATH = HERE / "index.html"

VALID_STATUS = frozenset({"live", "shadow"})
VALID_ACTOR = frozenset({"dag", "api", "telegram"})


def load_catalog(path: Path = CATALOG_PATH) -> list[dict]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("datasets"), list):
        raise ValueError("catalog.yaml must have a top-level 'datasets' list")
    datasets = []
    seen = set()
    for i, item in enumerate(raw["datasets"]):
        loc = f"datasets[{i}]"
        if not isinstance(item, dict):
            raise ValueError(f"{loc} must be a mapping")
        ds_id = item.get("id")
        status = item.get("status")
        if not ds_id or not isinstance(ds_id, str):
            raise ValueError(f"{loc}.id is required")
        if ds_id in seen:
            raise ValueError(f"duplicate dataset id: {ds_id}")
        if status not in VALID_STATUS:
            raise ValueError(f"{ds_id}.status must be live|shadow, got {status!r}")
        producers = _actors(item.get("producers") or [], f"{ds_id}.producers")
        consumers = _actors(item.get("consumers") or [], f"{ds_id}.consumers")
        if not producers:
            raise ValueError(f"{ds_id} needs at least one producer")
        seen.add(ds_id)
        datasets.append(
            {
                "id": ds_id,
                "status": status,
                "producers": producers,
                "consumers": consumers,
                "notes": (item.get("notes") or "").strip(),
            }
        )
    return datasets


def _actors(rows, loc: str) -> list[dict]:
    out = []
    seen = set()
    for j, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"{loc}[{j}] must be {{type, id}}")
        kind = row.get("type")
        aid = row.get("id")
        if kind not in VALID_ACTOR or not aid:
            raise ValueError(f"{loc}[{j}] needs type={{{'|'.join(sorted(VALID_ACTOR))}}} and id")
        key = (kind, aid)
        if key in seen:
            continue
        seen.add(key)
        out.append({"type": kind, "id": str(aid)})
    return out


def actor_key(kind: str, aid: str) -> str:
    return f"{kind}:{aid}"


def dataset_key(ds_id: str) -> str:
    return f"dataset:{ds_id}"


def build_graph(datasets: list[dict]) -> dict:
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    writes: dict[str, list[str]] = defaultdict(list)
    read_by: dict[str, list[str]] = defaultdict(list)
    written_by: dict[str, list[str]] = defaultdict(list)

    def add_actor(kind: str, aid: str) -> str:
        key = actor_key(kind, aid)
        if key not in nodes:
            nodes[key] = {
                "id": key,
                "kind": kind,
                "label": aid,
                "status": "",
            }
        return key

    for ds in datasets:
        dkey = dataset_key(ds["id"])
        nodes[dkey] = {
            "id": dkey,
            "kind": "dataset",
            "label": ds["id"],
            "status": ds["status"],
            "notes": ds["notes"],
        }
        for prod in ds["producers"]:
            pkey = add_actor(prod["type"], prod["id"])
            edges.append({"from": pkey, "to": dkey, "rel": "writes"})
            if dkey not in writes[pkey]:
                writes[pkey].append(dkey)
            if pkey not in written_by[dkey]:
                written_by[dkey].append(pkey)
        for cons in ds["consumers"]:
            ckey = add_actor(cons["type"], cons["id"])
            edges.append({"from": dkey, "to": ckey, "rel": "reads"})
            if ckey not in read_by[dkey]:
                read_by[dkey].append(ckey)

    return {
        "nodes": list(nodes.values()),
        "edges": edges,
        "writes": dict(writes),
        "readBy": dict(read_by),
        "writtenBy": dict(written_by),
        "datasetCount": sum(1 for n in nodes.values() if n["kind"] == "dataset"),
        "actorCount": sum(1 for n in nodes.values() if n["kind"] != "dataset"),
    }


def _downstream(graph: dict, dataset_id: str) -> tuple[set[str], set[str]]:
    datasets = {dataset_id}
    actors: set[str] = set()
    queue = [dataset_id]
    read_by = graph["readBy"]
    writes = graph["writes"]
    while queue:
        current = queue.pop()
        for consumer in read_by.get(current, []):
            if consumer in actors:
                continue
            actors.add(consumer)
            for nxt in writes.get(consumer, []):
                if nxt not in datasets:
                    datasets.add(nxt)
                    queue.append(nxt)
    return datasets, actors


def blast_radius(graph: dict, node_id: str) -> dict[str, set[str]]:
    """Full downstream + immediate upstream. Mirrors the HTML click contract."""
    kinds = {n["id"]: n["kind"] for n in graph["nodes"]}
    if node_id not in kinds:
        raise KeyError(node_id)
    datasets: set[str] = set()
    actors: set[str] = set()
    upstream: set[str] = set()
    writes = graph["writes"]
    read_by = graph["readBy"]
    written_by = graph["writtenBy"]

    def absorb_dataset(ds_id: str) -> None:
        datasets.add(ds_id)
        for producer in written_by.get(ds_id, []):
            upstream.add(producer)
            actors.add(producer)
        down_ds, down_actors = _downstream(graph, ds_id)
        datasets.update(down_ds)
        actors.update(down_actors)

    if kinds[node_id] == "dataset":
        absorb_dataset(node_id)
    else:
        actors.add(node_id)
        for ds_id in writes.get(node_id, []):
            absorb_dataset(ds_id)
        for ds_id, consumers in read_by.items():
            if node_id in consumers:
                absorb_dataset(ds_id)
    return {"datasets": datasets, "actors": actors, "upstream": upstream}


def render_html(graph: dict) -> str:
    payload = json.dumps(graph, ensure_ascii=False)
    return _TEMPLATE.replace("__GRAPH__", payload)


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="id">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Data lineage — kontrak produk hidup</title>
  <script src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
  <style>
    :root {
      --bg: #0b1020;
      --panel: #121a2f;
      --border: #24304f;
      --text: #e8edf7;
      --muted: #8b97b3;
      --dataset: #f5c16c;
      --dataset-shadow: #c4a36a;
      --dag: #5b9cff;
      --api: #3ecf8e;
      --telegram: #c084fc;
      --edge-write: #7d8aa8;
      --edge-read: #4a556e;
      --hot: #ff6b4a;
    }
    * { box-sizing: border-box; }
    html, body { margin: 0; height: 100%; background: var(--bg); color: var(--text);
      font-family: "IBM Plex Sans", "Segoe UI", sans-serif; }
    header {
      display: flex; align-items: center; gap: 16px;
      padding: 10px 16px; border-bottom: 1px solid var(--border);
      background: #0d1426;
    }
    header h1 { font-size: 15px; font-weight: 600; margin: 0; letter-spacing: .02em; }
    header input {
      margin-left: auto; width: 280px; background: var(--panel); color: var(--text);
      border: 1px solid var(--border); border-radius: 6px; padding: 7px 10px; font-size: 13px;
    }
    header button {
      background: transparent; color: var(--muted); border: 1px solid var(--border);
      border-radius: 6px; padding: 6px 10px; cursor: pointer; font-size: 12px;
    }
    header button:hover { color: var(--text); }
    .layout { display: flex; height: calc(100% - 52px); }
    #graph { flex: 1; min-width: 0; }
    aside {
      width: 340px; border-left: 1px solid var(--border); background: var(--panel);
      padding: 14px 16px; overflow: auto; font-size: 13px;
    }
    aside h2 { font-size: 13px; margin: 0 0 6px; color: var(--dataset); }
    aside .kind { font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }
    aside .notes { color: var(--muted); margin: 8px 0 14px; line-height: 1.45; }
    aside h3 { font-size: 11px; text-transform: uppercase; letter-spacing: .06em;
      color: var(--muted); margin: 16px 0 6px; }
    aside ul { list-style: none; padding: 0; margin: 0; }
    aside li { padding: 4px 0; border-bottom: 1px solid #1b243c; cursor: pointer; }
    aside li:hover { color: var(--dataset); }
    .legend { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 18px; font-size: 11px; color: var(--muted); }
    .swatch { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 5px; }
    .empty { color: var(--muted); margin-top: 24px; line-height: 1.5; }
    .count { color: var(--hot); font-weight: 600; }
  </style>
</head>
<body>
  <header>
    <h1>Data lineage</h1>
    <input id="search" type="search" placeholder="Cari dataset / DAG / API…" />
    <button type="button" id="clear">Clear</button>
  </header>
  <div class="layout">
    <div id="graph"></div>
    <aside id="panel">
      <div class="empty">Klik sebuah titik. Hilir transitif dan hulu langsung akan menyala.</div>
      <div class="legend">
        <span><i class="swatch" style="background:var(--dataset)"></i>dataset</span>
        <span><i class="swatch" style="background:var(--dag)"></i>DAG</span>
        <span><i class="swatch" style="background:var(--api)"></i>API</span>
        <span><i class="swatch" style="background:var(--telegram)"></i>Telegram</span>
      </div>
    </aside>
  </div>
  <script>
    const GRAPH = __GRAPH__;
    const writes = GRAPH.writes || {};
    const readBy = GRAPH.readBy || {};
    const writtenBy = GRAPH.writtenBy || {};

    const COLORS = {
      dataset: { live: "#f5c16c", shadow: "#c4a36a" },
      dag: "#5b9cff",
      api: "#3ecf8e",
      telegram: "#c084fc",
    };

    function nodeColor(n) {
      if (n.kind === "dataset") return n.status === "shadow" ? COLORS.dataset.shadow : COLORS.dataset.live;
      return COLORS[n.kind] || "#aaa";
    }

    const nodes = new vis.DataSet(GRAPH.nodes.map((n) => ({
      id: n.id,
      label: n.label,
      title: n.kind + (n.status ? " · " + n.status : ""),
      color: {
        background: nodeColor(n),
        border: nodeColor(n),
        highlight: { background: "#fff4d6", border: "#ff6b4a" },
      },
      font: { color: "#e8edf7", size: n.kind === "dataset" ? 13 : 11 },
      shape: n.kind === "dataset" ? "dot" : "dot",
      size: n.kind === "dataset" ? 18 : 11,
      borderWidth: n.status === "shadow" ? 3 : 1,
      borderDashes: n.status === "shadow" ? [4, 3] : false,
      kind: n.kind,
      status: n.status || "",
      notes: n.notes || "",
    })));

    const edges = new vis.DataSet(GRAPH.edges.map((e, i) => ({
      id: "e" + i,
      from: e.from,
      to: e.to,
      rel: e.rel,
      arrows: { to: { enabled: true, scaleFactor: 0.6 } },
      color: { color: e.rel === "writes" ? "#7d8aa8" : "#3a4563", opacity: 0.7 },
      dashes: e.rel === "reads",
      width: e.rel === "writes" ? 1.4 : 1,
      smooth: { type: "continuous", roundness: 0.15 },
    })));

    const network = new vis.Network(
      document.getElementById("graph"),
      { nodes, edges },
      {
        interaction: { hover: true, tooltipDelay: 120, hideEdgesOnDrag: true },
        physics: {
          solver: "forceAtlas2Based",
          forceAtlas2Based: {
            gravitationalConstant: -42,
            springLength: 110,
            springConstant: 0.06,
            avoidOverlap: 0.6,
          },
          stabilization: { iterations: 180 },
        },
        nodes: { shadow: { enabled: true, color: "rgba(0,0,0,.45)", size: 8 } },
      }
    );

    const nodeById = Object.fromEntries(GRAPH.nodes.map((n) => [n.id, n]));

    function downstreamFromDataset(dsId) {
      const datasets = new Set([dsId]);
      const actors = new Set();
      const queue = [dsId];
      while (queue.length) {
        const d = queue.shift();
        for (const c of readBy[d] || []) {
          if (actors.has(c)) continue;
          actors.add(c);
          for (const d2 of writes[c] || []) {
            if (!datasets.has(d2)) {
              datasets.add(d2);
              queue.push(d2);
            }
          }
        }
      }
      return { datasets, actors };
    }

    function blast(id) {
      const node = nodeById[id];
      if (!node) return null;
      const datasets = new Set();
      const actors = new Set();
      const upstream = new Set();

      if (node.kind === "dataset") {
        datasets.add(id);
        for (const p of writtenBy[id] || []) {
          upstream.add(p);
          actors.add(p);
        }
        const down = downstreamFromDataset(id);
        down.datasets.forEach((d) => datasets.add(d));
        down.actors.forEach((a) => actors.add(a));
      } else {
        actors.add(id);
        for (const d of writes[id] || []) {
          datasets.add(d);
          const down = downstreamFromDataset(d);
          down.datasets.forEach((x) => datasets.add(x));
          down.actors.forEach((a) => actors.add(a));
        }
        for (const [ds, cons] of Object.entries(readBy)) {
          if (cons.includes(id)) {
            datasets.add(ds);
            for (const p of writtenBy[ds] || []) {
              upstream.add(p);
              actors.add(p);
            }
            const down = downstreamFromDataset(ds);
            down.datasets.forEach((x) => datasets.add(x));
            down.actors.forEach((a) => actors.add(a));
          }
        }
      }
      return { datasets, actors, upstream, origin: id };
    }

    const allNodeIds = GRAPH.nodes.map((n) => n.id);
    const allEdgeIds = GRAPH.edges.map((_, i) => "e" + i);

    function applyHighlight(result) {
      const hot = new Set([...(result.datasets || []), ...(result.actors || [])]);
      nodes.update(allNodeIds.map((id) => {
        const n = nodeById[id];
        const on = hot.has(id);
        return {
          id,
          color: {
            background: on ? nodeColor(n) : "#1c2438",
            border: on ? (id === result.origin ? "#ff6b4a" : nodeColor(n)) : "#2a334c",
            highlight: { background: "#fff4d6", border: "#ff6b4a" },
          },
          font: { color: on ? "#e8edf7" : "#3d4a66", size: n.kind === "dataset" ? 13 : 11 },
          size: on && n.kind === "dataset" ? 20 : n.kind === "dataset" ? 16 : on ? 12 : 9,
        };
      }));
      edges.update(allEdgeIds.map((eid, i) => {
        const e = GRAPH.edges[i];
        const on = hot.has(e.from) && hot.has(e.to);
        return {
          id: eid,
          color: { color: on ? (e.rel === "writes" ? "#f5c16c" : "#8b97b3") : "#1c2438", opacity: on ? 1 : 0.15 },
          width: on ? 2 : 0.6,
        };
      }));
    }

    function resetHighlight() {
      nodes.update(allNodeIds.map((id) => {
        const n = nodeById[id];
        return {
          id,
          color: { background: nodeColor(n), border: nodeColor(n) },
          font: { color: "#e8edf7", size: n.kind === "dataset" ? 13 : 11 },
          size: n.kind === "dataset" ? 18 : 11,
        };
      }));
      edges.update(allEdgeIds.map((eid, i) => {
        const e = GRAPH.edges[i];
        return {
          id: eid,
          color: { color: e.rel === "writes" ? "#7d8aa8" : "#3a4563", opacity: 0.7 },
          width: e.rel === "writes" ? 1.4 : 1,
        };
      }));
    }

    function renderPanel(id, result) {
      const panel = document.getElementById("panel");
      const node = nodeById[id];
      const dsList = [...result.datasets].map((x) => nodeById[x]).filter(Boolean);
      const actorList = [...result.actors].map((x) => nodeById[x]).filter(Boolean);
      const upList = [...result.upstream].map((x) => nodeById[x]).filter(Boolean);
      const hit = dsList.length + actorList.length;
      const li = (n) =>
        `<li data-id="${n.id}"><span class="kind">${n.kind}</span> ${n.label}${n.status === "shadow" ? " · shadow" : ""}</li>`;
      panel.innerHTML = `
        <div class="kind">${node.kind}${node.status ? " · " + node.status : ""}</div>
        <h2>${node.label}</h2>
        ${node.notes ? `<div class="notes">${node.notes}</div>` : ""}
        <div>Blast radius: <span class="count">${hit}</span> node</div>
        <h3>Hulu langsung (${upList.length})</h3>
        <ul>${upList.length ? upList.map(li).join("") : "<li>—</li>"}</ul>
        <h3>Dataset terkena (${dsList.length})</h3>
        <ul>${dsList.map(li).join("")}</ul>
        <h3>Producer / consumer terkena (${actorList.length})</h3>
        <ul>${actorList.map(li).join("")}</ul>
        <div class="legend">
          <span><i class="swatch" style="background:var(--dataset)"></i>dataset</span>
          <span><i class="swatch" style="background:var(--dag)"></i>DAG</span>
          <span><i class="swatch" style="background:var(--api)"></i>API</span>
          <span><i class="swatch" style="background:var(--telegram)"></i>Telegram</span>
        </div>`;
      panel.querySelectorAll("li[data-id]").forEach((el) => {
        el.addEventListener("click", () => select(el.getAttribute("data-id")));
      });
    }

    function select(id) {
      const result = blast(id);
      if (!result) return;
      applyHighlight(result);
      renderPanel(id, result);
      network.selectNodes([id]);
      network.focus(id, { scale: Math.max(network.getScale(), 0.85), animation: { duration: 250 } });
    }

    network.on("click", (params) => {
      if (params.nodes.length) select(params.nodes[0]);
    });

    document.getElementById("clear").addEventListener("click", () => {
      resetHighlight();
      network.unselectAll();
      document.getElementById("panel").innerHTML = `
        <div class="empty">Klik sebuah titik. Hilir transitif dan hulu langsung akan menyala.</div>
        <div class="legend">
          <span><i class="swatch" style="background:var(--dataset)"></i>dataset</span>
          <span><i class="swatch" style="background:var(--dag)"></i>DAG</span>
          <span><i class="swatch" style="background:var(--api)"></i>API</span>
          <span><i class="swatch" style="background:var(--telegram)"></i>Telegram</span>
        </div>`;
    });

    document.getElementById("search").addEventListener("input", (ev) => {
      const q = ev.target.value.trim().toLowerCase();
      if (!q) return;
      const hit = GRAPH.nodes.find((n) => n.label.toLowerCase().includes(q) || n.id.toLowerCase().includes(q));
      if (hit) select(hit.id);
    });
  </script>
</body>
</html>
"""


def _assert_blast(graph: dict) -> None:
    pita = blast_radius(graph, dataset_key("pita_ma_snapshot"))
    expect_ds = {
        dataset_key("pita_ma_snapshot"),
        dataset_key("peluang_berlapis_snapshot"),
        dataset_key("ready_besok_snapshot"),
    }
    missing = expect_ds - pita["datasets"]
    if missing:
        raise AssertionError(f"pita_ma blast missing datasets: {missing}")
    if dataset_key("setup_snapshot") in pita["datasets"]:
        raise AssertionError("pita_ma must not touch setup_snapshot")
    for actor in (
        actor_key("dag", "peluang_berlapis_daily"),
        actor_key("dag", "ready_besok_daily"),
        actor_key("api", "/api/v1/radar-momentum"),
        actor_key("api", "/api/v1/peluang-2"),
    ):
        if actor not in pita["actors"]:
            raise AssertionError(f"pita_ma blast missing {actor}")

    setup = blast_radius(graph, dataset_key("setup_snapshot"))
    if dataset_key("peluang_berlapis_snapshot") in setup["datasets"]:
        raise AssertionError("setup_snapshot must not flow into Peluang Berlapis")
    if dataset_key("belum_dihargai_snapshot") not in setup["datasets"]:
        raise AssertionError("setup_snapshot should reach belum_dihargai_snapshot")


def main(argv: list[str] | None = None) -> int:
    _ = argv
    datasets = load_catalog()
    graph = build_graph(datasets)
    _assert_blast(graph)
    HTML_PATH.write_text(render_html(graph), encoding="utf-8")
    print(
        f"Wrote {HTML_PATH.name} "
        f"({graph['datasetCount']} datasets, {graph['actorCount']} actors, "
        f"{len(graph['edges'])} edges)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
