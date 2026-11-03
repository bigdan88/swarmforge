"""Shared progress messages for Projects and Work."""

from swarmforge.runqueue import PAUSED_KEY, lane_paused, worker_pid
from swarmforge.projects.service import lane


def describe(store, project, tasks):
    if project["last_error"]:
        return project["last_error"]
    if project["status"] != "running":
        return None
    if store.kv_get(PAUSED_KEY) == "1":
        return "The queue is paused. Start the queue to continue."
    if not worker_pid(store):
        return "The queue worker is offline. Start it to continue this project."
    activity = store.kv_get(f"project_activity:{project['id']}")
    if activity:
        return f"{activity}. Tasks will appear on Work when the plan is saved."
    running = next((task for task in tasks if task["status"] == "running"), None)
    if running:
        return f"Working on {running['title']}. Follow the task on Work."
    if lane_paused(store, lane(project["id"])):
        return "This project's queue is paused. Resume its lane on Work."
    if any(task["status"] == "queued" for task in tasks):
        return "Tasks are ready on Work and waiting for a queue slot."
    return "Waiting for the worker to prepare the next plan. No new tasks have been queued yet."
