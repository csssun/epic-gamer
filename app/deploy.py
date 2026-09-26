"""CLI: --login initializes a local session; default runs one collection."""
import argparse
import asyncio
import json
import os
import sys
from contextlib import suppress

from loguru import logger
from camoufox import AsyncCamoufox

from services.epic_authorization_service import EpicAuthorization
from services.epic_games_service import EpicAgent
from settings import LOG_DIR, RUNTIME_DIR, SCREENSHOTS_DIR, settings
from utils import init_log


async def execute_browser_tasks(headless=None, login_only=False, interactive=False, export_state=None):
    headless = settings.HEADLESS if headless is None else headless
    if login_only or interactive:
        headless = False
    async with AsyncCamoufox(
        persistent_context=True,
        user_data_dir=str(settings.user_data_dir),
        headless=headless,
        locale='en-US',
    ) as context:
        page = context.pages[0] if context.pages else await context.new_page()
        page.set_default_timeout(30000)
        page.set_default_navigation_timeout(60000)
        try:
            async with asyncio.timeout(settings.TASK_TIMEOUT_SECONDS):
                if not await EpicAuthorization(page, manual=login_only or interactive).invoke():
                    raise RuntimeError('Authentication failed')
                logger.success('AUTHENTICATED: verified store session')
                if export_state:
                    export_state.parent.mkdir(parents=True, exist_ok=True)
                    await context.storage_state(path=str(export_state))
                    os.chmod(export_state, 0o600)
                    logger.info('Session exported locally; treat it as an account credential')
                if login_only:
                    return []
                results = await EpicAgent(page, manual=interactive).collect_epic_games()
                RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
                (RUNTIME_DIR / 'result.json').write_text(
                    json.dumps({'status': 'completed', 'games': results}, ensure_ascii=False, indent=2),
                    encoding='utf-8',
                )
                return results
        except Exception as exc:
            RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            (RUNTIME_DIR / 'result.json').write_text(json.dumps({
                'status': 'failed', 'error_type': type(exc).__name__, 'message': str(exc),
            }, ensure_ascii=False, indent=2), encoding='utf-8')
            if settings.SAVE_SCREENSHOTS:
                with suppress(Exception):
                    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=str(SCREENSHOTS_DIR / 'failure.png'),
                                          mask=[page.locator('input')])
            raise
        finally:
            await context.close()


async def deploy(args):
    await execute_browser_tasks(login_only=args.login, interactive=args.interactive,
                                export_state=args.export_state)
    if not settings.ENABLE_APSCHEDULER or args.login:
        return
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    scheduler = AsyncIOScheduler(timezone='Asia/Shanghai')
    scheduler.add_job(execute_browser_tasks, 'cron', day_of_week='fri,mon', hour=11, minute=30,
                      max_instances=1, coalesce=True, misfire_grace_time=3600)
    scheduler.start()
    try:
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown(wait=False)


def main():
    from pathlib import Path
    parser = argparse.ArgumentParser()
    parser.add_argument('--login', action='store_true', help='Log in interactively and save local profile')
    parser.add_argument('--interactive', action='store_true', help='Allow manual login and checkout prompts')
    parser.add_argument('--export-state', type=Path, help='Export verified session for private CI Secret')
    args = parser.parse_args()
    init_log(runtime=LOG_DIR / 'runtime.log', error=LOG_DIR / 'error.log')
    try:
        asyncio.run(deploy(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        # Loguru exception diagnostics can expose local variables; do not use them here.
        logger.error('{}: {}', type(exc).__name__, str(exc))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
