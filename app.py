from pathlib import Path
import uvicorn


from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pydantic import BaseModel
from backend import run_travel_agent

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

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return template.TemplateResponse(request, "index.html")

@app.post("/api/plan")
def plan(req: PlanRequest):
    try:
        return run_travel_agent(req.query, req.thread_id)
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)


if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host="127.0.0.1",
        port = 8000,
        reload = True
    )