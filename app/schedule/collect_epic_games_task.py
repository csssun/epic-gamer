"""Celery tasks must be synchronous wrappers around the async browser workflow."""
import asyncio
from deploy import execute_browser_tasks
from extensions.ext_celery import ext_celery_app


@ext_celery_app.task(queue='epic-awesome-gamer')
def collect_epic_games_task():
    return asyncio.run(execute_browser_tasks())


if __name__ == '__main__':
    collect_epic_games_task()
