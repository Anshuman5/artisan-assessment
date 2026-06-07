"""SQLite persistence for sender profiles and target evaluations."""
import json
import sqlite3
import time
import uuid
from pathlib import Path

DB_PATH = Path(__file__).parent / "data.db"


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = _conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS senders (
            id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            domain TEXT,
            company_name TEXT,
            one_liner TEXT,
            data TEXT NOT NULL,        -- full JSON: profile + evidence + meta
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS evaluations (
            id TEXT PRIMARY KEY,
            sender_id TEXT NOT NULL,
            target_url TEXT NOT NULL,
            target_name TEXT,
            persona_role TEXT,
            persona_seniority TEXT,
            fit_score INTEGER,
            data TEXT NOT NULL,        -- full JSON result
            created_at REAL NOT NULL
        );
        """
    )
    c.commit()
    c.close()


def save_sender(url: str, result: dict) -> str:
    sid = "snd_" + uuid.uuid4().hex[:12]
    profile = result.get("profile", {})
    c = _conn()
    c.execute(
        "INSERT INTO senders (id,url,domain,company_name,one_liner,data,created_at) VALUES (?,?,?,?,?,?,?)",
        (sid, url, result.get("meta", {}).get("domain"),
         profile.get("company_name") or result.get("meta", {}).get("company_name"),
         profile.get("one_liner"), json.dumps(result), time.time()),
    )
    c.commit()
    c.close()
    return sid


def get_sender(sid: str) -> dict | None:
    c = _conn()
    row = c.execute("SELECT * FROM senders WHERE id=?", (sid,)).fetchone()
    c.close()
    if not row:
        return None
    d = json.loads(row["data"])
    d["id"] = row["id"]
    d["created_at"] = row["created_at"]
    return d


def list_senders() -> list[dict]:
    c = _conn()
    rows = c.execute("SELECT id,url,domain,company_name,one_liner,created_at FROM senders ORDER BY created_at DESC").fetchall()
    c.close()
    return [dict(r) for r in rows]


def save_evaluation(sender_id: str, target_url: str, persona_role: str,
                    persona_seniority: str, result: dict) -> str:
    eid = "evl_" + uuid.uuid4().hex[:12]
    c = _conn()
    c.execute(
        "INSERT INTO evaluations (id,sender_id,target_url,target_name,persona_role,persona_seniority,fit_score,data,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (eid, sender_id, target_url, result.get("target_name"), persona_role, persona_seniority,
         (result.get("fit") or {}).get("fit_score"), json.dumps(result), time.time()),
    )
    c.commit()
    c.close()
    return eid


def get_evaluation(eid: str) -> dict | None:
    c = _conn()
    row = c.execute("SELECT * FROM evaluations WHERE id=?", (eid,)).fetchone()
    c.close()
    if not row:
        return None
    d = json.loads(row["data"])
    d["id"] = row["id"]
    d["sender_id"] = row["sender_id"]
    d["created_at"] = row["created_at"]
    return d


def list_evaluations(sender_id: str | None = None) -> list[dict]:
    c = _conn()
    if sender_id:
        rows = c.execute(
            "SELECT id,sender_id,target_url,target_name,persona_role,persona_seniority,fit_score,created_at FROM evaluations WHERE sender_id=? ORDER BY created_at DESC",
            (sender_id,)).fetchall()
    else:
        rows = c.execute(
            "SELECT id,sender_id,target_url,target_name,persona_role,persona_seniority,fit_score,created_at FROM evaluations ORDER BY created_at DESC").fetchall()
    c.close()
    return [dict(r) for r in rows]
