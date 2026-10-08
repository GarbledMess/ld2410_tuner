"""Cleanup for server-owned tasks whose callers may leave before completion."""


def finish_task(tasks, key, task):
    """Retire only this task, preserving any replacement started under the same key."""
    if tasks.get(key) is task:
        tasks.pop(key)
    # Retrieve failures even without a waiting browser; each job owns its report.
    if not task.cancelled():
        task.exception()
