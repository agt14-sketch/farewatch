from fastapi import APIRouter, BackgroundTasks
from app.scripts.run_scheduler import run_scheduler_once   

router = APIRouter()

@router.post("/tasks/run_scheduler")
def tasks_run_scheduler(background_tasks: BackgroundTasks):
    """
    Trigger the scheduler logic once asynchronously.
    Used by Render Cron Jobs.
    """
    background_tasks.add_task(run_scheduler_once)
    return {"status": "accepted"}
