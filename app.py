"""FastAPI app: serves the web UI and exposes POST /api/plan, which runs the LangGraph pipeline in backend.py.
"""

from pathlib import Path
import uvicorn


from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pydantic import BaseModel
from backend import run_travel_agent, resume_travel_agent


import nest_asyncio
nest_asyncio.apply()

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(
    title= "AI travel planning sysytem",
    description="Langgraph multi agent ttravel planner with fastapi frontend",
    version="1.0.0"
)

app.mount(
    "/static",
    StaticFiles(directory=str(BASE_DIR/ "static")),
    name = "static"
)

template = Jinja2Templates(
    directory=str(BASE_DIR/"templates"),
)

class PlanRequest(BaseModel):
    query: str
    thread_id: str | None = None
    start_date: str | None = None  # "YYYY-MM-DD" from the date picker

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    """Serve the single-page UI (templates/index.html)."""
    return template.TemplateResponse(request, "index.html")

@app.post("/api/plan")
def plan(req: PlanRequest):
    """Run the TripMate graph for one request and return the plan.

    Any error is returned as a 500 with {"detail": ...}.
    """
    try:
        return run_travel_agent(req.query, req.thread_id, req.start_date)
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)


class ApproveRequest(BaseModel):
    thread_id: str
    approved: bool
    feedback: str = ""


@app.post("/api/approve")
def approve(req: ApproveRequest):
    """Resume a paused trip with the user's Approve / Request-changes answer."""
    try:
        return resume_travel_agent(req.thread_id, req.approved, req.feedback)
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)


if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host="127.0.0.1",
        port = 8000,
        reload = True
    )