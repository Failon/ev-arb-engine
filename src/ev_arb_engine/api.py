from importlib.resources import files

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import AwareDatetime, Field

from .models import Quote, StrictModel
from .replay import ReplayDataset, replay
from .reports import ReportRequest, report
from .research import analyze

app = FastAPI(title="ev-arb-engine", description="Offline research only; no bet placement")


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return files("ev_arb_engine").joinpath("dashboard.html").read_text(encoding="utf-8")


@app.post("/reports")
def historical_report(request: ReportRequest):
    return report(request)


class AnalyzeRequest(StrictModel):
    quotes: list[Quote] = Field(min_length=3, max_length=1000)
    as_of: AwareDatetime
    budget: float = Field(default=100, gt=0)


@app.get("/health")
def health():
    return {"status": "ok", "mode": "research-only"}


@app.post("/replay")
def replay_history(request: ReplayDataset):
    return replay(request)


@app.post("/analyze")
def analyze_quotes(request: AnalyzeRequest):
    try:
        return analyze(request.quotes, request.as_of, request.budget)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
