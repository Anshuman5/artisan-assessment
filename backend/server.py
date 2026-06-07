"""OutboundIQ FastAPI backend."""
import asyncio
import traceback

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pathlib import Path

import db
import agent

app = FastAPI(title="OutboundIQ")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

db.init_db()


class SenderReq(BaseModel):
    url: str


class TargetReq(BaseModel):
    sender_id: str
    target_url: str
    persona_role: str
    persona_seniority: str


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/sender/analyze")
async def sender_analyze(req: SenderReq):
    try:
        result = await agent.analyze_sender(req.url)
        if not result.get("ok"):
            raise HTTPException(status_code=422, detail=result.get("error", "Analysis failed"))
        sid = db.save_sender(req.url, result)
        result["id"] = sid
        return result
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.get("/api/senders")
def senders():
    return {"senders": db.list_senders()}


@app.get("/api/sender/{sid}")
def sender_get(sid: str):
    s = db.get_sender(sid)
    if not s:
        raise HTTPException(status_code=404, detail="Not found")
    return s


@app.post("/api/target/evaluate")
async def target_evaluate(req: TargetReq):
    sender = db.get_sender(req.sender_id)
    if not sender:
        raise HTTPException(status_code=404, detail="Sender profile not found")
    try:
        result = await agent.evaluate_target(
            sender.get("profile", {}), req.target_url,
            req.persona_role, req.persona_seniority,
        )
        if not result.get("ok"):
            raise HTTPException(status_code=422, detail=result.get("error", "Evaluation failed"))
        eid = db.save_evaluation(req.sender_id, req.target_url,
                                 req.persona_role, req.persona_seniority, result)
        result["id"] = eid
        result["sender_id"] = req.sender_id
        return result
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.get("/api/evaluations")
def evaluations(sender_id: str | None = None):
    return {"evaluations": db.list_evaluations(sender_id)}


@app.get("/api/evaluation/{eid}")
def evaluation_get(eid: str):
    e = db.get_evaluation(eid)
    if not e:
        raise HTTPException(status_code=404, detail="Not found")
    return e


# ---- Static frontend (built SPA) ----
DIST = Path(__file__).parent.parent / "frontend" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/")
    def index():
        return FileResponse(DIST / "index.html")

    @app.get("/{path:path}")
    def spa(path: str):
        f = DIST / path
        if f.exists() and f.is_file():
            return FileResponse(f)
        return FileResponse(DIST / "index.html")
