from fastapi import APIRouter, BackgroundTasks, HTTPException
from app.logic.scheduler_worker import run_scheduler_once 

from scripts.send_onboarding_batch import main as run_onboarding_once

router = APIRouter()

@router.post("/tasks/run_scheduler")
def tasks_run_scheduler(background_tasks: BackgroundTasks):
    background_tasks.add_task(run_scheduler_once)
    return {"status": "scheduled"}

@router.post("/tasks/send_onboarding")
def tasks_send_onboarding(background_tasks: BackgroundTasks):
    background_tasks.add_task(run_onboarding_once)
    return {"status": "scheduled"}
