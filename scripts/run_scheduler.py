import time
from apscheduler.schedulers.blocking import BlockingScheduler
import pytz

import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scripts.snapshot_all_watches import main as run_all_watches

EST = pytz.timezone("America/New_York")


def job():
    print(f"[scheduler] starting job for ALL watches at {time.strftime('%Y-%m-%d %H:%M', time.localtime())}")
    run_all_watches()
    print("[scheduler] job complete")


def main():
    sched = BlockingScheduler(timezone="America/New_York")
    # run every 12 hours
    sched.add_job(job, "interval", hours=12)
    print("[scheduler] first run will execute immediately")
    job()  # optional: run once on startup

    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        print("[scheduler] stopped.")


if __name__ == "__main__":
    main()