"""可选 Web 前端的 FastAPI 后端。

复用核心流水线与落盘状态，不改变 CLI 行为：
- 持久 run 列表：GET /api/runs 扫 state_dir 列出所有书+进度+状态（关浏览器/重启可见）；
- 上传：POST /api/upload?name= 原始字节直传（浏览器手动选文件，命令行无需指定）；
- 读态 REST：state / glossary / chapter / report / revisions；
- 术语写：编辑 / 删除 / 裁决冲突 / 应用到正文；
- 运行：POST /api/run 在后台线程跑 Orchestrator.run_steps（步骤可选/全选），run_id = run-dir slug；
- 暂停：POST /api/stop 协作式停在批边界（已落盘，再次运行即续跑）；
- WebSocket /ws/{slug}：实时推送批次级双语对照/建议/进度；无活动任务回 idle，前端改用 REST 读盘。
  任务为后台线程，关浏览器不影响其继续；重连=REST 读盘 + WS 接增量。
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any, Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import Config
from ..glossary.store import GlossaryStore, GlossaryTerm
from ..glossary import resolver
from ..agents.glossary_auditor import GlossaryAuditor
from ..ingest.segmenter import load_document
from ..pipeline.orchestrator import Orchestrator, RunCancelled
from ..pipeline.runstore import RunStore, slugify, STATUS_DONE

_STATIC = os.path.join(os.path.dirname(__file__), "static")

# input 路径 → run_dir 缓存（避免每次读态都重新解析整本书）
_RUN_DIR_CACHE: dict[str, str] = {}


def _scan_run_dir(config: Config, abs_input: str) -> Optional[str]:
    """在 state_dir 各 run 目录里按 manifest.source_path 反查 run_dir（避免重解析整本书）。"""
    root = config.state_dir
    if not os.path.isdir(root):
        return None
    for name in os.listdir(root):
        d = os.path.join(root, name)
        mf = os.path.join(d, "manifest.json")
        if not os.path.isfile(mf):
            continue
        try:
            m = RunStore._read_json(mf)
        except Exception:
            continue
        sp = m.get("source_path", "")
        if sp and os.path.abspath(sp) == abs_input:
            return d
    return None


def _run_dir(config: Config, input_path: str) -> str:
    key = os.path.abspath(input_path)
    cached = _RUN_DIR_CACHE.get(key)
    if cached:
        return cached
    # 先按已有 manifest 反查（快）；查不到再解析文档取标题作 slug（新书首次）
    run_dir = _scan_run_dir(config, key)
    if run_dir is None:
        doc = load_document(input_path, config.source_lang, config.target_lang)
        run_dir = os.path.join(config.state_dir, slugify(doc.title))
    _RUN_DIR_CACHE[key] = run_dir
    return run_dir


def _store(config: Config, input_path: str) -> RunStore:
    return RunStore(_run_dir(config, input_path))


# ── 运行管理：后台线程 + 事件队列桥接到 asyncio ──────────────────────────────
# 以 run-dir 的 slug 为持久 run_id：关闭/重连浏览器都按 slug 找回正在跑的任务；
# 任务是后台线程，关浏览器不影响其继续；进度内容均落盘，重连用 REST 读盘 + WS 接增量。
class _Run:
    def __init__(self) -> None:
        # 有界队列：WS 正常会立刻消费；万一没有消费者也不会无限堆积内存
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=4096)
        self.done = False
        self.cancel = threading.Event()      # 协作式停止信号
        self.steps: list[str] = []


class RunManager:
    def __init__(self) -> None:
        self.runs: dict[str, _Run] = {}      # slug -> _Run（仅含正在运行的任务）

    def is_running(self, slug: str) -> bool:
        return slug in self.runs

    def stop(self, slug: str) -> bool:
        run = self.runs.get(slug)
        if run is None:
            return False
        run.cancel.set()
        return True

    def start(self, loop: asyncio.AbstractEventLoop, config: Config,
              run_dir: str, input_path: str, steps, out_format: str,
              out_path: Optional[str]) -> Optional[str]:
        slug = os.path.basename(run_dir.rstrip("/"))
        if slug in self.runs:
            return None  # 该书已有任务在跑，不重复启动
        run = _Run()
        run.steps = list(steps)
        self.runs[slug] = run

        def _put(ev: dict) -> None:
            try:
                run.queue.put_nowait(ev)
            except asyncio.QueueFull:
                pass  # 无消费者导致积压 → 丢弃，避免内存无限增长

        def emit(ev: dict) -> None:
            try:
                loop.call_soon_threadsafe(_put, ev)
            except RuntimeError:
                pass  # 事件循环已关闭（如服务关停）→ 丢弃事件

        def worker() -> None:
            try:
                Orchestrator(config).run_steps(
                    input_path, steps, events=emit,
                    out_format=out_format, out_path=out_path,
                    should_stop=run.cancel.is_set)
            except RunCancelled:
                emit({"type": "paused"})
            except Exception as e:  # 把异常也推给前端
                emit({"type": "error", "detail": str(e)})
            finally:
                run.done = True
                emit({"type": "_end"})
                self.runs.pop(slug, None)     # 任务结束才移除（WS 断开不移除）

        threading.Thread(target=worker, daemon=True).start()
        return slug


# ── 请求体 ──────────────────────────────────────────────────────────────────
class RunReq(BaseModel):
    input: str
    steps: list[str]
    format: str = "epub"
    out: Optional[str] = None


class TermReq(BaseModel):
    input: str
    source: str
    target: str = ""
    type: Optional[str] = None
    gender: Optional[str] = None
    lock: bool = True
    apply_to_text: bool = True   # 编辑译法时把旧译法在正文里改写为新译法


class DeleteReq(BaseModel):
    input: str
    source: str


class ResolveReq(BaseModel):
    input: str
    source: str
    target: str
    apply_to_text: bool = False


class ApplyReq(BaseModel):
    input: str
    source: str


class InputReq(BaseModel):
    input: str


def _glossary_payload(g: GlossaryStore) -> dict[str, Any]:
    return {
        "terms": [
            {"source": t.source, "target": t.target, "reading": t.reading,
             "type": t.type, "gender": t.gender, "aliases": t.aliases,
             "confidence": t.confidence, "locked": t.locked, "status": t.status}
            for t in g.all_terms()
        ],
        "conflicts": g.open_conflicts(),
    }


def create_app(config_path: str = "config.yaml",
               default_input: Optional[str] = None) -> FastAPI:
    app = FastAPI(title="trans-novel web")
    manager = RunManager()

    def cfg() -> Config:
        return Config.load(config_path)

    # ── 页面 ────────────────────────────────────────────────────────────────
    @app.get("/")
    def index():
        return FileResponse(os.path.join(_STATIC, "index.html"))

    @app.get("/api/config")
    def get_config():
        return {"default_input": default_input, "steps": list(Orchestrator.ALL_STEPS)}

    # ── 持久 run 列表（扫 state_dir，关浏览器/重启服务后都能看到并续跑）────────────
    @app.get("/api/runs")
    def runs():
        config = cfg()
        root = config.state_dir
        out: list[dict] = []
        if os.path.isdir(root):
            for name in sorted(os.listdir(root)):
                mf = os.path.join(root, name, "manifest.json")
                if not os.path.isfile(mf):
                    continue
                try:
                    m = RunStore._read_json(mf)
                except Exception:
                    continue
                chs = m.get("chapters", [])
                total = len(chs)
                done = sum(1 for c in chs if c.get("status") == STATUS_DONE)
                running = manager.is_running(name)
                if running:
                    status = "running"
                elif total and done == total:
                    status = "done"
                elif done > 0:
                    status = "partial"
                else:
                    status = "pending"
                sp = m.get("source_path", "")
                out.append({
                    "slug": name,
                    "title": m.get("title", ""),
                    "title_translated": m.get("title_translated", ""),
                    "input": sp,
                    "source_exists": bool(sp) and os.path.isfile(sp),
                    "fmt": m.get("fmt", ""),
                    "source_lang": m.get("source_lang", ""),
                    "total": total, "done": done, "status": status,
                    "running": running,
                    "steps": manager.runs[name].steps if running else [],
                })
        return {"runs": out}

    # ── 上传书籍（浏览器手动选文件；原始字节直传，存到 state_dir/_uploads）─────────
    @app.post("/api/upload")
    async def upload(request: Request, name: str):
        config = cfg()
        data = await request.body()
        if not data:
            return JSONResponse({"error": "empty body"}, status_code=400)
        safe = os.path.basename(name) or "upload.bin"
        updir = os.path.join(config.state_dir, "_uploads")
        os.makedirs(updir, exist_ok=True)
        path = os.path.join(updir, safe)
        with open(path, "wb") as f:
            f.write(data)
        return {"input": os.path.abspath(path), "name": safe}

    # ── 读态 ────────────────────────────────────────────────────────────────
    @app.get("/api/state")
    def state(input: str):
        store = _store(cfg(), input)
        if not store.exists():
            return JSONResponse({"exists": False})
        m = store.load_manifest()
        g = GlossaryStore(store.glossary_path)
        stats = g.stats()
        g.close()
        analysis = store.load_analysis() or {}
        return {
            "exists": True, "title": m["title"],
            "title_translated": m.get("title_translated", ""),
            "fmt": m["fmt"],
            "source_lang": m["source_lang"], "target_lang": m["target_lang"],
            "chapters": [{"index": c["index"], "title": c["title"],
                          "title_translated": c.get("title_translated", ""),
                          "status": c["status"]} for c in m["chapters"]],
            "glossary_stats": stats,
            "analysis": {"genre": analysis.get("genre", ""),
                         "tone": analysis.get("tone", ""),
                         "style_guide": analysis.get("style_guide", ""),
                         "characters": analysis.get("characters", [])},
        }

    @app.get("/api/glossary")
    def glossary(input: str):
        store = _store(cfg(), input)
        if not store.exists():
            return JSONResponse({"terms": [], "conflicts": []})
        g = GlossaryStore(store.glossary_path)
        data = _glossary_payload(g)
        g.close()
        return data

    @app.get("/api/chapter")
    def chapter(input: str, index: int):
        store = _store(cfg(), input)
        if not store.exists():
            return JSONResponse({"segments": []})
        ch = store.load_chapter(index)
        m = store.load_manifest()
        ct = next((c.get("title_translated", "") for c in m["chapters"]
                   if c["index"] == index), "")
        return {
            "index": ch.index, "title": ch.title, "title_translated": ct,
            "segments": [{"source": s.source, "target": s.target or "", "kind": s.kind}
                         for s in ch.segments if s.source.strip()],
        }

    @app.get("/api/report")
    def report(input: str):
        store = _store(cfg(), input)
        if not store.exists() or not os.path.isfile(store.report_path):
            return JSONResponse({})
        return RunStore._read_json(store.report_path)

    @app.get("/api/revisions")
    def revisions(input: str):
        store = _store(cfg(), input)
        if not store.exists():
            return JSONResponse({"review": [], "backtranslation": [],
                                 "consistency": [], "unifications": []})
        m = store.load_manifest()
        review: list[dict] = []
        bt: list[dict] = []
        for c in m["chapters"]:
            try:
                ch = store.load_chapter(c["index"])
            except Exception:
                continue
            review.extend(ch.meta.get("review_issues", []))
            bt.extend(ch.meta.get("backtranslation_issues", []))
        rep = {}
        if os.path.isfile(store.report_path):
            rep = RunStore._read_json(store.report_path)
        return {
            "review": review, "backtranslation": bt,
            "consistency": rep.get("consistency_issues", []),
            "unifications": rep.get("glossary_unifications", []),
        }

    # ── 术语写 ──────────────────────────────────────────────────────────────
    @app.put("/api/glossary/term")
    def put_term(req: TermReq):
        store = _store(cfg(), req.input)
        g = GlossaryStore(store.glossary_path)
        try:
            existing = g.get_term(req.source)
            old_target = existing.target if existing else ""
            new_target = req.target or old_target
            if existing is None:
                g.upsert_term(GlossaryTerm(
                    source=req.source, target=req.target,
                    type=req.type or "术语", gender=req.gender or "",
                    confidence="high", locked=req.lock))
            elif req.lock or existing.locked:
                g.lock_term(req.source, new_target)
            else:
                g.upsert_term(GlossaryTerm(
                    source=req.source, target=new_target,
                    type=req.type or existing.type,
                    gender=req.gender if req.gender is not None else existing.gender,
                    confidence="high"))
            g.mark_conflicts_resolved(req.source)
            # 译法被改动 → 把旧译法在正文里改写为新译法
            rewritten = 0
            if req.apply_to_text and old_target and new_target and old_target != new_target:
                rewritten = GlossaryAuditor._rewrite_targets(
                    store, g, {old_target: new_target})
            data = _glossary_payload(g)
            data["rewritten"] = rewritten
        finally:
            g.close()
        return data

    @app.delete("/api/glossary/term")
    def delete_term(req: DeleteReq):
        store = _store(cfg(), req.input)
        g = GlossaryStore(store.glossary_path)
        try:
            g.delete_term(req.source)
            data = _glossary_payload(g)
        finally:
            g.close()
        return data

    @app.post("/api/glossary/resolve")
    def resolve_conflict(req: ResolveReq):
        store = _store(cfg(), req.input)
        g = GlossaryStore(store.glossary_path)
        try:
            old = g.get_term(req.source)
            old_target = old.target if old else ""
            resolver.resolve(g, req.source, req.target)
            rewritten = 0
            if req.apply_to_text and old_target and old_target != req.target:
                rewritten = GlossaryAuditor._rewrite_targets(
                    store, g, {old_target: req.target})
            data = _glossary_payload(g)
            data["rewritten"] = rewritten
        finally:
            g.close()
        return data

    @app.post("/api/glossary/reapply")
    def reapply_all(req: InputReq):
        """把整张术语表重新应用到正文：所有术语的别名/变体统一改写为其当前译法。"""
        store = _store(cfg(), req.input)
        g = GlossaryStore(store.glossary_path)
        try:
            replace: dict[str, str] = {}
            for t in g.all_terms():
                for a in t.aliases:
                    if a and a != t.target:
                        replace[a] = t.target
            rewritten = GlossaryAuditor._rewrite_targets(store, g, replace) if replace else 0
            data = _glossary_payload(g)
            data["rewritten"] = rewritten
        finally:
            g.close()
        return data

    @app.post("/api/glossary/apply")
    def apply_term(req: ApplyReq):
        store = _store(cfg(), req.input)
        g = GlossaryStore(store.glossary_path)
        try:
            t = g.get_term(req.source)
            rewritten = 0
            if t and t.aliases:
                replace = {a: t.target for a in t.aliases if a and a != t.target}
                if replace:
                    rewritten = GlossaryAuditor._rewrite_targets(store, g, replace)
            data = _glossary_payload(g)
            data["rewritten"] = rewritten
        finally:
            g.close()
        return data

    @app.get("/api/glossary/occurrences")
    def occurrences(input: str, source: str):
        """术语溯源：列出该词（含别名）在全书原文/译文里出现的位置。"""
        store = _store(cfg(), input)
        if not store.exists():
            return JSONResponse({"occurrences": []})
        g = GlossaryStore(store.glossary_path)
        term = g.get_term(source)
        g.close()
        keys = [source] + (term.aliases if term else [])
        keys = [k for k in keys if k]
        out: list[dict] = []
        m = store.load_manifest()
        for c in m["chapters"]:
            try:
                ch = store.load_chapter(c["index"])
            except Exception:
                continue
            for s in ch.text_segments:
                if any(k in s.source for k in keys):
                    out.append({"chapter": c["index"], "title": ch.title,
                                "index": s.index, "source": s.source,
                                "target": s.target or ""})
        return {"source": source, "keys": keys, "count": len(out), "occurrences": out}

    @app.post("/api/consistency/fix")
    def consistency_fix(req: InputReq):
        """一致性自动修复（仅术语/译名类，可安全全局替换）。代词/语气留作建议。"""
        from ..agents.consistency import ConsistencyChecker
        from ..llm.base import build_client

        config = cfg()
        store = _store(config, req.input)
        if not store.exists():
            return JSONResponse({"replacements": [], "rewritten": 0})
        m = store.load_manifest()
        config.source_lang = m.get("source_lang") or config.source_lang
        g = GlossaryStore(store.glossary_path)
        try:
            result = ConsistencyChecker(build_client(config), config).autofix(store, g)
        finally:
            g.close()
        return result

    # ── 运行 + WebSocket ──────────────────────────────────────────────────────
    @app.post("/api/run")
    async def run(req: RunReq):
        loop = asyncio.get_running_loop()
        steps = [s for s in req.steps if s in Orchestrator.ALL_STEPS]
        if not steps:
            return JSONResponse({"error": "no valid steps"}, status_code=400)
        if not os.path.isfile(req.input):
            return JSONResponse({"error": "input not found"}, status_code=400)
        config = cfg()
        run_dir = _run_dir(config, req.input)
        run_id = manager.start(loop, config, run_dir, req.input, steps,
                               req.format, req.out)
        if run_id is None:
            return JSONResponse({"error": "already running"}, status_code=409)
        return {"run_id": run_id, "steps": steps}

    @app.post("/api/stop")
    def stop(req: InputReq):
        """协作式暂停：在批边界优雅停下（已译部分已落盘，再次运行即续跑）。"""
        run_dir = _run_dir(cfg(), req.input)
        slug = os.path.basename(run_dir.rstrip("/"))
        return {"stopping": manager.stop(slug), "slug": slug}

    @app.websocket("/ws/{run_id}")
    async def ws(websocket: WebSocket, run_id: str):
        # run_id = run-dir 的 slug。无活动任务 → 告知 idle（前端改用 REST 读盘）。
        await websocket.accept()
        run = manager.runs.get(run_id)
        if run is None:
            await websocket.send_json({"type": "idle"})
            await websocket.close()
            return
        try:
            while True:
                ev = await run.queue.get()
                if ev.get("type") == "_end":
                    await websocket.send_json({"type": "end"})
                    break
                await websocket.send_json(ev)
        except WebSocketDisconnect:
            pass
        # 不在此移除 run：关浏览器后任务仍在跑，留待 worker 结束时清理，支持重连

    app.mount("/static", StaticFiles(directory=_STATIC), name="static")
    return app
