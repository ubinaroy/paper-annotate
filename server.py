#!/usr/bin/env python3
"""paper-annotate：在浏览器里对论文 PDF 逐句 / 逐区域写批注的本地小服务（只依赖 Python 3.9+ 标准库）。

  python3 server.py path/to/main.pdf [--tex-dir DIR] [--data DIR] [--port 8791]   # 即 serve，可省略
  python3 server.py serve path/to/main.pdf ...
  python3 server.py new-round  [--data DIR]          # 用重新编译后的 PDF 开新一轮
  python3 server.py reply <轮次> <编号> "回复" [--by 名字] [--status done|discuss|open] [--data DIR]
  python3 server.py md <轮次> [--data DIR]             # 重新生成并打印 annotations.md

每一“轮”对应一份 PDF 快照（连同 synctex 与 .tex 源码），论文重新编译后旧批注的位置仍然对得上。
批注存在 <data>/<轮次>/annotations.json，并生成 annotations.md（可直接交给 Claude 等 AI 改稿）。
可选依赖：synctex（TeX 自带，反查 .tex 行号）、poppler 的 pdfinfo / pdftoppm（页面尺寸、区域截图）。
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

try:
    import fcntl
except ImportError:  # Windows：只做进程内互斥
    fcntl = None

HERE = Path(__file__).resolve().parent
ROUND_RE = re.compile(r"^R\d{2,}_[0-9A-Za-z_.-]+$")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
EXPORT_FORMAT = "paper-annotate/1"

# 字段归属：浏览器写的字段 / 服务端写的字段（synctex、截图、回复）
CLIENT_FIELDS = ("n", "kind", "page", "rects", "quote", "ctx", "comment", "tag", "author", "created", "updated", "deleted")
SERVER_FIELDS = ("source", "crop", "reply", "reply_at", "reply_by", "_geo")
STATUS_LABEL = {"open": "未处理", "done": "已改", "discuss": "待讨论"}
KIND_LABEL = {"sentence": "句子", "text": "选中文字", "region": "区域", "global": "整体意见"}


class Config:
    data: Path = Path("annotate-data").resolve()
    pdf: Path | None = None
    tex_dir: Path | None = None
    author: str = ""


CFG = Config()
_tlock = threading.Lock()


def has(tool: str) -> bool:
    return shutil.which(tool) is not None


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


@contextlib.contextmanager
def locked(rdir: Path):
    with _tlock, open(rdir / ".lock", "w") as fh:
        if fcntl:
            fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl:
                fcntl.flock(fh, fcntl.LOCK_UN)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), "utf-8")
    os.replace(tmp, path)


def sha1(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def page_sizes(pdf: Path) -> list[list[float]]:
    """有 pdfinfo 就用它；没有的话返回空，由浏览器用 pdf.js 量好再报上来。"""
    if not has("pdfinfo"):
        return []
    out = subprocess.run(["pdfinfo", "-f", "1", "-l", "9999", str(pdf)], capture_output=True, text=True).stdout
    sizes = [(int(m[1]), float(m[2]), float(m[3])) for m in re.finditer(r"^Page\s+(\d+) size:\s+([\d.]+) x ([\d.]+)", out, re.M)]
    return [[w, h] for _, w, h in sorted(sizes)]


# ---------------------------------------------------------------- project / rounds

def load_project() -> None:
    """命令行没给 PDF 时，沿用数据目录里记下的 PDF 与源码目录。"""
    proj = read_json(CFG.data / "project.json", {})
    if CFG.pdf is None and proj.get("pdf"):
        CFG.pdf = Path(proj["pdf"])
    if CFG.tex_dir is None and proj.get("tex_dir"):
        CFG.tex_dir = Path(proj["tex_dir"])
    if not CFG.author:
        CFG.author = proj.get("author", "")
    if CFG.pdf is not None and CFG.tex_dir is None:
        CFG.tex_dir = CFG.pdf.parent
    if CFG.pdf is not None:
        CFG.data.mkdir(parents=True, exist_ok=True)
        new = {"pdf": str(CFG.pdf), "tex_dir": str(CFG.tex_dir), **({"author": CFG.author} if CFG.author else {})}
        if new != proj:
            write_json(CFG.data / "project.json", new)


def list_rounds() -> list[dict]:
    if not CFG.data.exists():
        return []
    metas = []
    for d in sorted(CFG.data.iterdir()):
        if d.is_dir() and ROUND_RE.match(d.name) and (d / "meta.json").exists():
            metas.append(read_json(d / "meta.json", {}))
    return metas


def round_dir(name: str) -> Path:
    if not ROUND_RE.match(name or ""):
        raise KeyError(name)
    d = CFG.data / name
    if not (d / "meta.json").exists():
        raise KeyError(name)
    return d


def live_status() -> dict:
    pdf = CFG.pdf
    if pdf is None or not pdf.exists():
        return {"exists": False, "path": str(pdf)}
    st = pdf.stat()
    info = {"exists": True, "path": str(pdf), "mtime": dt.datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="seconds"), "size": st.st_size}
    rounds = list_rounds()
    if rounds:
        last = rounds[-1]
        same = last.get("pdf_size") == st.st_size and last.get("pdf_mtime_raw") == st.st_mtime
        if not same:
            same = sha1(pdf) == last.get("pdf_sha1")
        info["changed_since_last_round"] = not same
    else:
        info["changed_since_last_round"] = True
    return info


def new_round(force: bool = False, note: str = "") -> tuple[dict, bool]:
    pdf = CFG.pdf
    if pdf is None or not pdf.exists():
        raise FileNotFoundError(pdf)
    digest = sha1(pdf)
    rounds = list_rounds()
    if rounds and rounds[-1].get("pdf_sha1") == digest and not force:
        return rounds[-1], False
    idx = max([int(re.match(r"R(\d+)", r["name"])[1]) for r in rounds] + [0]) + 1
    st = pdf.stat()
    mtime = dt.datetime.fromtimestamp(st.st_mtime)
    name = f"R{idx:02d}_{mtime:%Y%m%d-%H%M}"
    rdir = CFG.data / name
    rdir.mkdir(parents=True)
    shutil.copy2(pdf, rdir / pdf.name)
    syn = pdf.with_name(f"{pdf.stem}.synctex.gz")
    if syn.exists():
        shutil.copy2(syn, rdir / syn.name)
    # 源码快照：之后改稿会让行号漂移，留一份当轮的 .tex 方便对照
    tex_dir = CFG.tex_dir or pdf.parent
    src, copied = rdir / "src", 0
    for p in tex_dir.rglob("*"):
        if copied >= 2000:
            break
        if p.is_file() and p.suffix in (".tex", ".bib") and not any(part.startswith(".") for part in p.relative_to(tex_dir).parts):
            dst = src / p.relative_to(tex_dir)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
            copied += 1
    meta = {
        "name": name,
        "created": now_iso(),
        "paper_dir": str(tex_dir),
        "pdf_src": str(pdf),
        "pdf_file": pdf.name,
        "pdf_sha1": digest,
        "pdf_size": st.st_size,
        "pdf_mtime_raw": st.st_mtime,
        "pdf_mtime": mtime.astimezone().isoformat(timespec="seconds"),
        "synctex": syn.exists() and has("synctex"),
        "page_sizes": page_sizes(rdir / pdf.name),
        "note": note,
    }
    write_json(rdir / "meta.json", meta)
    write_json(rdir / "annotations.json", {"round": name, "updated": now_iso(), "items": []})
    write_md(rdir, meta, [])
    return meta, True


def set_page_sizes(rdir: Path, sizes) -> dict:
    meta = read_json(rdir / "meta.json", {})
    ok = isinstance(sizes, list) and sizes and all(isinstance(s, list) and len(s) == 2 and all(isinstance(v, (int, float)) for v in s) for s in sizes)
    if ok and not meta.get("page_sizes"):
        with locked(rdir):
            meta["page_sizes"] = [[float(w), float(h)] for w, h in sizes]
            write_json(rdir / "meta.json", meta)
    return meta


# ---------------------------------------------------------------- synctex / crops

def synctex_at(pdf: Path, paper_dir: Path, page: int, x: float, y: float) -> str | None:
    try:
        out = subprocess.run(["synctex", "edit", "-o", f"{page}:{x:.2f}:{y:.2f}:{pdf}"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m_in = re.search(r"^Input:(.+)$", out, re.M)
    m_ln = re.search(r"^Line:(\d+)", out, re.M)
    if not (m_in and m_ln):
        return None
    path = os.path.normpath(m_in[1].strip())
    try:
        path = os.path.relpath(path, paper_dir)
    except ValueError:
        pass
    return f"{path}:{m_ln[1]}"


def compress_sources(locs: list[str]) -> list[str]:
    """['sec/3.tex:20', 'sec/3.tex:22'] -> ['sec/3.tex:20-22']"""
    by_file: dict[str, list[int]] = {}
    for loc in locs:
        if not loc:
            continue
        f, _, ln = loc.rpartition(":")
        by_file.setdefault(f, []).append(int(ln))
    out = []
    for f, lines in by_file.items():
        lo, hi = min(lines), max(lines)
        out.append(f"{f}:{lo}" if lo == hi else f"{f}:{lo}-{hi}")
    return out


def _words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", s.lower())


def _has_run(hay: list[str], pat: list[str]) -> bool:
    n = len(pat)
    return any(hay[i:i + n] == pat for i in range(len(hay) - n + 1))


def find_quote_line(lines: list[str], words: list[str], approx: int, from_end: bool) -> int | None:
    """synctex 对段落只报段尾行；用引文开头（或结尾）几个词在源码里找确切的行，取离 synctex 行最近、且不在其后的命中。"""
    toks = [_words(re.sub(r"(?<!\\)%.*", "", ln)) for ln in lines]
    k = min(4, len(words))
    if k == 0:
        return None
    for shift in range(0, min(9, len(words) - k + 1)):
        pat = words[len(words) - k - shift: len(words) - shift] if from_end else words[shift: shift + k]
        hits = [i + 1 for i, t in enumerate(toks) if _has_run(t, pat)]
        if hits:
            return min(hits, key=lambda h: abs(h - approx) + (0 if h <= approx + 1 else 200))
    return None


def refine_sources(locs: list[str], quote_text: str, rdir: Path, paper_dir: Path) -> list[str]:
    words = _words(quote_text)
    out = []
    start_hit = None
    for loc, from_end in zip(locs, (False, True)):
        if not loc:
            continue
        f, _, ln = loc.rpartition(":")
        src = rdir / "src" / f
        if not src.exists():
            src = paper_dir / f
        try:
            lines = src.read_text("utf-8", errors="replace").splitlines()
        except OSError:
            out.append(loc)
            continue
        hit = find_quote_line(lines, words, int(ln), from_end) if words else None
        if hit is None and from_end and start_hit and start_hit[0] == f:
            hit = start_hit[1]          # 结尾对不上（多半是 \cref 之类）：认开头那行
        if not from_end and hit:
            start_hit = (f, hit)
        out.append(f"{f}:{hit or ln}")
    return out


def enrich(item: dict, rdir: Path, meta: dict) -> None:
    """为带位置的批注补 synctex 源码行；区域批注再补一张截图。几何没变就不重算。"""
    rects = item.get("rects") or []
    if not rects or item.get("deleted"):
        return
    geo = hashlib.sha1(json.dumps(rects).encode()).hexdigest()[:12]
    if item.get("_geo") == geo:
        return
    pdf = rdir / meta["pdf_file"]
    sizes = meta.get("page_sizes") or []
    paper_dir = Path(meta["paper_dir"])

    def pt(p, x, y):
        w, h = sizes[p - 1] if 0 < p <= len(sizes) else (612.0, 792.0)
        return p, x * w, y * h

    locs = []
    if meta.get("synctex") and has("synctex"):
        if item.get("kind") == "region":
            # 区域只探中心点（角点常落到浮动体或别处，行号范围会被拉得很宽）
            p, x0, y0, x1, y1 = rects[0]
            probes = [pt(p, (x0 + x1) / 2, (y0 + y1) / 2)]
        else:
            first, last = rects[0], rects[-1]
            probes = [pt(first[0], first[1] + 0.004, (first[2] + first[4]) / 2), pt(last[0], last[3] - 0.004, (last[2] + last[4]) / 2)]
        locs = [synctex_at(pdf, paper_dir, *pr) for pr in probes]
        if item.get("kind") != "region" and item.get("quote"):
            locs = refine_sources(locs, item["quote"], rdir, paper_dir)
        elif item.get("kind") == "region" and locs and locs[0] and len(_words(item.get("quote", ""))) >= 6:
            # 框住的是成段文字时，用区域内开头几个词把起始行也找出来
            f, _, ln = locs[0].rpartition(":")
            start = refine_sources([locs[0]], item["quote"], rdir, paper_dir)[0]
            s_ln = int(start.rpartition(":")[2])
            if 0 < int(ln) - s_ln <= 40:
                locs = [start, locs[0]]
    item["source"] = compress_sources([l for l in locs if l])
    if item.get("kind") == "region" and has("pdftoppm"):
        p, x0, y0, x1, y1 = rects[0]
        w, h = sizes[p - 1] if 0 < p <= len(sizes) else (612.0, 792.0)
        dpi = 150
        s = dpi / 72
        pad = 4
        X, Y = max(0, int((x0 * w - pad) * s)), max(0, int((y0 * h - pad) * s))
        W, H = int(((x1 - x0) * w + 2 * pad) * s), int(((y1 - y0) * h + 2 * pad) * s)
        crops = rdir / "crops"
        crops.mkdir(exist_ok=True)
        stem = crops / item["id"]
        subprocess.run(["pdftoppm", "-f", str(p), "-l", str(p), "-r", str(dpi), "-x", str(X), "-y", str(Y), "-W", str(W), "-H", str(H), "-png", "-singlefile", str(pdf), str(stem)], capture_output=True, timeout=30)
        if stem.with_suffix(".png").exists():
            item["crop"] = f"crops/{item['id']}.png"
    item["_geo"] = geo


# ---------------------------------------------------------------- merge / import / markdown

def merge(disk_items: list[dict], client_items: list[dict]) -> list[dict]:
    disk = {it["id"]: it for it in disk_items if "id" in it}
    order = [it["id"] for it in disk_items if "id" in it]
    for c in client_items:
        cid = c.get("id")
        if not isinstance(cid, str) or not ID_RE.match(cid):
            continue
        d = disk.get(cid)
        if d is None:
            d = {"id": cid}
            disk[cid] = d
            order.append(cid)
        if c.get("updated", "") >= d.get("updated", ""):
            for k in CLIENT_FIELDS:
                if k in c:
                    d[k] = c[k]
        if c.get("status_at", "") >= d.get("status_at", ""):
            d["status"] = c.get("status", "open")
            d["status_at"] = c.get("status_at", "")
        d.setdefault("status", "open")
    return [disk[i] for i in order]


def import_items(rdir: Path, incoming: list[dict]) -> dict:
    """合并别人导出的批注：新条目追加（重新编号），同一条目按更新时间取新。"""
    meta = read_json(rdir / "meta.json", {})
    added = changed = 0
    with locked(rdir):
        data = read_json(rdir / "annotations.json", {"round": meta.get("name"), "items": []})
        items = data.get("items", [])
        by_id = {it["id"]: it for it in items if "id" in it}
        next_n = max([it.get("n", 0) for it in items] + [0]) + 1
        for c in incoming:
            cid = c.get("id") if isinstance(c, dict) else None
            if not isinstance(cid, str) or not ID_RE.match(cid):
                continue
            d = by_id.get(cid)
            if d is None:
                if c.get("deleted"):
                    continue
                d = {k: c[k] for k in (*CLIENT_FIELDS, "status", "status_at", "reply", "reply_at", "reply_by") if k in c}
                d["id"], d["n"] = cid, next_n
                d.setdefault("status", "open")
                next_n += 1
                items.append(d)
                by_id[cid] = d
                added += 1
                continue
            touched = False
            if c.get("updated", "") > d.get("updated", ""):
                for k in CLIENT_FIELDS:
                    if k in c and k != "n":
                        d[k] = c[k]
                touched = True
            if c.get("status_at", "") > d.get("status_at", ""):
                d["status"], d["status_at"] = c.get("status", "open"), c["status_at"]
                touched = True
            if c.get("reply") and c.get("reply_at", "") > d.get("reply_at", ""):
                d["reply"], d["reply_at"], d["reply_by"] = c["reply"], c["reply_at"], c.get("reply_by", "")
                touched = True
            changed += touched
        for it in items:
            enrich(it, rdir, meta)
        data["items"] = items
        data["updated"] = now_iso()
        write_json(rdir / "annotations.json", data)
        write_md(rdir, meta, items)
    return {"added": added, "updated": changed}


def export_doc(rdir: Path) -> dict:
    meta = read_json(rdir / "meta.json", {})
    data = read_json(rdir / "annotations.json", {"items": []})
    items = [{k: v for k, v in it.items() if k not in ("_geo", "crop")} for it in data.get("items", []) if not it.get("deleted")]
    return {"format": EXPORT_FORMAT, "round": meta.get("name"), "pdf_file": meta.get("pdf_file"), "pdf_sha1": meta.get("pdf_sha1"),
            "exported": now_iso(), "authors": sorted({it.get("author") for it in items if it.get("author")}), "items": items}


def sort_key(it: dict):
    if it.get("kind") == "global" or not it.get("rects"):
        return (0, 0, 0, 0, it.get("n", 0))
    p, x0, y0, *_ = it["rects"][0]
    return (1, p, 0 if x0 < 0.5 else 1, y0, it.get("n", 0))


def write_md(rdir: Path, meta: dict, items: list[dict]) -> str:
    live = [it for it in items if not it.get("deleted")]
    live.sort(key=sort_key)
    cnt = {k: sum(1 for it in live if it.get("status", "open") == k) for k in STATUS_LABEL}
    authors = sorted({it.get("author") for it in live if it.get("author")})
    lines = [
        f"# 论文批注 · {meta['name']}",
        "",
        f"- PDF 快照：`{rdir / meta['pdf_file']}`（源 `{meta['pdf_src']}`，编译于 {meta['pdf_mtime'][:16].replace('T', ' ')}，{len(meta.get('page_sizes', []))} 页）",
    ]
    if meta.get("synctex"):
        lines.append(f"- 当轮源码快照：`{rdir / 'src'}`；`file:line` 由 synctex 反查，指向**当轮**源码，改稿后行号可能漂移，以引文为准")
    lines += [
        f"- 共 {len(live)} 条：" + " · ".join(f"{STATUS_LABEL[k]} {cnt[k]}" for k in STATUS_LABEL) + (f"；批注人：{'、'.join(authors)}" if authors else ""),
        "",
    ]
    cur = None
    for it in live:
        group = "整体意见" if sort_key(it)[0] == 0 else f"第 {it['rects'][0][0]} 页"
        if group != cur:
            lines += [f"## {group}", ""]
            cur = group
        head = [f"#{it.get('n', '?')}", KIND_LABEL.get(it.get("kind"), it.get("kind", ""))]
        if it.get("author"):
            head.append(f"@{it['author']}")
        if it.get("source"):
            head.append(", ".join(f"`{s}`" for s in it["source"]))
        if it.get("tag"):
            head.append(f"[{it['tag']}]")
        head.append(STATUS_LABEL.get(it.get("status", "open"), it.get("status")))
        lines.append("### " + " · ".join(head))
        q = (it.get("quote") or "").strip()
        if q:
            if it.get("kind") == "region":
                lines.append(f"> （区域内文字）{q[:600]}{'…' if len(q) > 600 else ''}")
            else:
                lines.append("> " + q)
        if it.get("crop"):
            lines.append(f"区域截图：`{rdir / it['crop']}`")
        lines.append("")
        lines.append((it.get("comment") or "").strip() or "（无文字）")
        if it.get("reply"):
            who = it.get("reply_by") or "回复"
            lines += ["", f"**{who}**（{(it.get('reply_at') or '')[:16].replace('T', ' ')}）：{it['reply']}"]
        lines.append("")
    text = "\n".join(lines).rstrip() + "\n"
    (rdir / "annotations.md").write_text(text, "utf-8")
    return text


def save_round(rdir: Path, client_items: list[dict] | None = None) -> dict:
    meta = read_json(rdir / "meta.json", {})
    with locked(rdir):
        data = read_json(rdir / "annotations.json", {"round": meta.get("name"), "items": []})
        items = data.get("items", [])
        if client_items is not None:
            items = merge(items, client_items)
        for it in items:
            enrich(it, rdir, meta)
        data["items"] = items
        data["updated"] = now_iso()
        write_json(rdir / "annotations.json", data)
        write_md(rdir, meta, items)
    return data


def set_reply(name: str, n: int, text: str, status: str | None, by: str) -> dict:
    rdir = round_dir(name)
    meta = read_json(rdir / "meta.json", {})
    with locked(rdir):
        data = read_json(rdir / "annotations.json", {"items": []})
        hit = [it for it in data["items"] if it.get("n") == n and not it.get("deleted")]
        if not hit:
            raise SystemExit(f"{name} 里没有 #{n}")
        it = hit[0]
        it["reply"], it["reply_at"], it["reply_by"] = text, now_iso(), by
        if status:
            it["status"] = status
            it["status_at"] = now_iso()
        data["updated"] = now_iso()
        write_json(rdir / "annotations.json", data)
        write_md(rdir, meta, data["items"])
    return it


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "PaperAnnotate/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("ANNOTATE_VERBOSE"):
            super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200, extra=None):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8", extra)

    def _file(self, path: Path, ctype: str):
        if not path.exists():
            return self._json({"error": "not found"}, 404)
        self._send(200, path.read_bytes(), ctype)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _route(self):
        u = urlparse(self.path)
        return [p for p in u.path.split("/") if p], parse_qs(u.query)

    def do_GET(self):
        parts, _ = self._route()
        try:
            if not parts or parts == ["index.html"]:
                return self._file(HERE / "index.html", "text/html; charset=utf-8")
            if parts == ["api", "rounds"]:
                return self._json({"rounds": list_rounds(), "rounds_dir": str(CFG.data), "live": live_status(),
                                   "tools": {t: has(t) for t in ("synctex", "pdfinfo", "pdftoppm")}, "default_author": CFG.author})
            if len(parts) >= 4 and parts[:2] == ["api", "r"]:
                rdir = round_dir(parts[2])
                meta = read_json(rdir / "meta.json", {})
                what = parts[3]
                if what == "pdf":
                    return self._file(rdir / meta["pdf_file"], "application/pdf")
                if what == "annotations":
                    return self._json(read_json(rdir / "annotations.json", {"items": []}))
                if what == "md":
                    return self._send(200, (rdir / "annotations.md").read_bytes(), "text/markdown; charset=utf-8")
                if what == "export":
                    fname = f"annotations-{meta.get('name')}.json"
                    return self._json(export_doc(rdir), extra={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"})
                if what == "crops" and len(parts) == 5 and re.match(r"^[A-Za-z0-9_-]+\.png$", parts[4]):
                    return self._file(rdir / "crops" / parts[4], "image/png")
            return self._json({"error": "not found"}, 404)
        except KeyError:
            return self._json({"error": "no such round"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_PUT(self):
        parts, _ = self._route()
        try:
            if len(parts) == 4 and parts[:2] == ["api", "r"] and parts[3] == "annotations":
                rdir = round_dir(parts[2])
                items = self._body().get("items")
                if not isinstance(items, list):
                    return self._json({"error": "items must be a list"}, 400)
                return self._json(save_round(rdir, items))
            return self._json({"error": "not found"}, 404)
        except KeyError:
            return self._json({"error": "no such round"}, 404)

    def do_POST(self):
        parts, qs = self._route()
        try:
            if parts == ["api", "rounds"]:
                body = self._body()
                try:
                    meta, created = new_round(force=bool(body.get("force")), note=str(body.get("note", ""))[:200])
                except FileNotFoundError as e:
                    return self._json({"error": f"找不到 PDF：{e}"}, 400)
                return self._json({"round": meta, "created": created})
            if len(parts) == 4 and parts[:2] == ["api", "r"] and parts[3] == "page-sizes":
                rdir = round_dir(parts[2])
                return self._json(set_page_sizes(rdir, self._body().get("sizes")))
            if parts == ["api", "import"]:
                doc = self._body()
                items = doc.get("items") if isinstance(doc, dict) else None
                if not isinstance(items, list):
                    return self._json({"error": "不是 paper-annotate 导出的批注文件"}, 400)
                # 优先导进同一份 PDF（sha1 相同）的最新一轮；找不到就导进当前打开的轮次，并提示位置可能对不上
                same = [r for r in list_rounds() if doc.get("pdf_sha1") and r.get("pdf_sha1") == doc.get("pdf_sha1")]
                target = same[-1]["name"] if same else (qs.get("round") or [""])[0]
                rdir = round_dir(target)
                res = import_items(rdir, items)
                return self._json({"round": target, "mismatch": not same, **res})
            return self._json({"error": "not found"}, 404)
        except KeyError:
            return self._json({"error": "no such round"}, 404)
        except json.JSONDecodeError:
            return self._json({"error": "文件不是合法的 JSON"}, 400)


COMMANDS = ("serve", "new-round", "reply", "md")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # 省略子命令时默认 serve：`python3 server.py paper.pdf` 与 `python3 server.py` 都直接打开网页
    if not argv or (argv[0] not in COMMANDS and argv[0] not in ("-h", "--help")):
        argv.insert(0, "serve")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data", type=Path, default=Path("annotate-data"), help="批注数据目录（默认 ./annotate-data）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve", parents=[common], help="启动批注网页")
    sp.add_argument("pdf", nargs="?", type=Path, help="论文 PDF（第一次必填，之后沿用）")
    sp.add_argument("--tex-dir", type=Path, help="LaTeX 源码目录（默认 PDF 所在目录）")
    sp.add_argument("--author", help="默认署名（记在数据目录里；浏览器里改过署名的以浏览器为准）")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8791)
    nr = sub.add_parser("new-round", parents=[common], help="用重新编译后的 PDF 开新一轮")
    nr.add_argument("--force", action="store_true")
    nr.add_argument("--note", default="")
    rp = sub.add_parser("reply", parents=[common], help="给某条批注写回复")
    rp.add_argument("round")
    rp.add_argument("n", type=int)
    rp.add_argument("text")
    rp.add_argument("--by", default="Claude", help="回复人（默认 Claude）")
    rp.add_argument("--status", choices=list(STATUS_LABEL))
    md = sub.add_parser("md", parents=[common], help="重新生成并打印 annotations.md")
    md.add_argument("round")
    args = ap.parse_args(argv)

    CFG.data = args.data.expanduser().resolve()
    if getattr(args, "pdf", None):
        CFG.pdf = args.pdf.expanduser().resolve()
    if getattr(args, "tex_dir", None):
        CFG.tex_dir = args.tex_dir.expanduser().resolve()
    if getattr(args, "author", None):
        CFG.author = args.author.strip()[:40]
    load_project()

    if args.cmd == "new-round":
        meta, created = new_round(force=args.force, note=args.note)
        print(("新建 " if created else "PDF 没变，沿用 ") + meta["name"])
        return
    if args.cmd in ("reply", "md"):
        try:
            round_dir(args.round)
        except KeyError:
            names = ", ".join(r["name"] for r in list_rounds()) or "（还没有）"
            raise SystemExit(f"没有这一轮：{args.round}。数据目录 {CFG.data} 里有：{names}")
    if args.cmd == "reply":
        it = set_reply(args.round, args.n, args.text, args.status, args.by)
        print(f"#{it['n']} → {STATUS_LABEL.get(it.get('status', 'open'))}")
        return
    if args.cmd == "md":
        rdir = round_dir(args.round)
        save_round(rdir)
        print((rdir / "annotations.md").read_text("utf-8"))
        return

    if CFG.pdf is None and not list_rounds():
        ap.error("第一次启动要给出论文 PDF：python3 server.py serve path/to/main.pdf")
    CFG.data.mkdir(parents=True, exist_ok=True)
    if not list_rounds():
        meta, _ = new_round()
        print(f"首轮：{meta['name']}")
    port = args.port
    for _ in range(20):
        try:
            httpd = ThreadingHTTPServer((args.host, port), Handler)
            break
        except OSError:
            port += 1
    else:
        raise SystemExit(f"端口 {args.port}–{port} 都被占用了")
    missing = [t for t in ("synctex", "pdfinfo", "pdftoppm") if not has(t)]
    print(f"论文批注：http://localhost:{port}", flush=True)
    print(f"  PDF：{CFG.pdf}\n  数据：{CFG.data}", flush=True)
    if missing:
        print(f"  （没找到 {', '.join(missing)}：{'不反查 .tex 行号；' if 'synctex' in missing else ''}{'区域批注不截图' if 'pdftoppm' in missing else ''}，其余功能照常）", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
