"""Job queue endpoints: list, inspect, cancel."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from sqlmodel import Session, select

from storybored.db import get_session
from storybored.models import Job

router = APIRouter(prefix="/api", tags=["jobs"])

TERMINAL = {"done", "failed", "cancelled"}


@router.get("/jobs")
def list_jobs(status: str | None = None, session: Session = Depends(get_session)):
    query = select(Job)
    if status:
        query = query.where(Job.status == status)
    query = query.order_by(Job.id.desc()).limit(200)  # type: ignore[attr-defined]
    return jsonable_encoder(session.exec(query).all())


@router.get("/jobs/{job_id}")
def get_job(job_id: int, session: Session = Depends(get_session)):
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return jsonable_encoder(job)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int, request: Request, session: Session = Depends(get_session)):
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status in TERMINAL:
        raise HTTPException(status_code=409, detail=f"job already {job.status}")
    updated = request.app.state.runner.cancel(job_id)
    return jsonable_encoder(updated if updated is not None else job)
