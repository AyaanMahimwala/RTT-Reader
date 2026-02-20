"""
FastAPI server — thin wrapper around the shared agent in agent.py.

Usage:
    uvicorn api:app --reload
    curl -X POST http://localhost:8000/query -H "Content-Type: application/json" \
         -d '{"question": "Who did I hang out with the most?"}'
"""

from typing import Optional

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import run_agent
from db import list_memories, delete_memory

app = FastAPI(title="Calendar Query API", version="2.0.0")


# ──────────────────────────────────────────────
# API Models & Endpoint
# ──────────────────────────────────────────────

class QueryRequest(BaseModel):
    question: str
    session_id: Optional[str] = None


class QueryResponse(BaseModel):
    answer: str
    sql_queries: list[str]
    data: list[dict]
    session_id: str


@app.post("/query", response_model=QueryResponse)
async def query_calendar(request: QueryRequest):
    """Answer a natural language question about the calendar data."""
    result = run_agent(request.question, request.session_id)
    return QueryResponse(**result)


@app.get("/memories")
async def get_memories():
    """Return all saved memories."""
    return list_memories()


@app.delete("/memories/{memory_id}")
async def remove_memory(memory_id: str):
    """Delete a memory by id."""
    if delete_memory(memory_id):
        return {"status": "deleted"}
    return {"status": "not_found"}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def root():
    return RedirectResponse(url="/static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
