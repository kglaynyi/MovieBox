import asyncio
import logging
import secrets
from traceback import format_exc

from pyrogram import idle
from starlette.middleware.sessions import SessionMiddleware

from Backend import __version__, db
from Backend.fastapi import server
from Backend.fastapi.main import app
from Backend.helper import subscription_task_manager
from Backend.helper.link_checker import DeadLinkChecker
from Backend.helper.pinger import ping
from Backend.helper.pyro import restart_notification, setup_bot_commands
from Backend.helper.scan_manager import (
    dbcheck_manager,
    duplicate_manager,
    gdrive_scan_manager,
    scan_manager,
)
from Backend.helper.session_auth import get_active_session_string
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER
import Backend.pyrofork.bot as botmod
from Backend.pyrofork.bot import StreamBot
from Backend.pyrofork.clients import initialize_clients

loop = asyncio.get_event_loop()


#----- Boot every subsystem then idle the bot
async def start_services():
    try:
        LOGGER.info(f"Initializing Telegram-Stremio v-{__version__}")

        await db.connect()

        await SettingsManager.initialize(db)
        app.add_middleware(SessionMiddleware, secret_key=SettingsManager.current().session_secret or secrets.token_hex(32))
        # Bind HTTP before slow Telegram setup. Requests stay gated until ready.
        web_task = loop.create_task(server.serve())

        await scan_manager.load(db)
        dbcheck_manager.bind_db(db)
        gdrive_scan_manager.bind_db(db)
        duplicate_manager.bind_db(db)

        await db.reload_extra_databases(SettingsManager.current().extra_databases)

        await StreamBot.start()
        StreamBot.username = StreamBot.me.username
        LOGGER.info(f"Bot Client : [@{StreamBot.username}]")

        if botmod.Userbot is None:
            stored_session = await get_active_session_string()
            if stored_session:
                botmod.build_userbot(stored_session)
                LOGGER.info("Loaded Userbot session from encrypted storage.")

        if botmod.Userbot is not None:
            await botmod.Userbot.start()
            botmod.Userbot.username = botmod.Userbot.me.username
            LOGGER.info(f"Userbot Client : [@{botmod.Userbot.username}]")
        else:
            LOGGER.info("Userbot not configured — running with StreamBot only.")

        LOGGER.info("Initializing Multi Clients...")
        await initialize_clients()

        app.state.services_ready = True
        loop.create_task(_post_start())
        loop.create_task(ping())

        link_checker_task = DeadLinkChecker(db, app, check_interval_hours=24)
        loop.create_task(link_checker_task.start())

        LOGGER.info("Telegram-Stremio Started Successfully!")
        idle_task = loop.create_task(idle())
        done, _ = await asyncio.wait({web_task, idle_task}, return_when=asyncio.FIRST_COMPLETED)
        if web_task in done:
            await web_task
            if not server.should_exit:
                raise RuntimeError("Web server stopped unexpectedly")
    except Exception:
        LOGGER.error("Error during startup:\n" + format_exc())
        raise


async def _post_start():
    # Telegram notifications must not keep the web application from becoming ready.
    for action in (lambda: setup_bot_commands(StreamBot), restart_notification,
                   lambda: subscription_task_manager.sync(StreamBot)):
        try:
            await asyncio.wait_for(action(), timeout=20)
        except Exception:
            LOGGER.warning("Optional post-start task failed:\n" + format_exc())


#----- Cancel pending tasks and shut clients down
async def stop_services():
    try:
        LOGGER.info("Stopping services...")

        pending_tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in pending_tasks:
            task.cancel()
        await asyncio.gather(*pending_tasks, return_exceptions=True)

        await StreamBot.stop()
        if botmod.Userbot is not None:
            await botmod.Userbot.stop()

        await db.disconnect()
        LOGGER.info("Services stopped successfully.")
    except Exception:
        LOGGER.error("Error during shutdown:\n" + format_exc())


if __name__ == '__main__':
    try:
        loop.run_until_complete(start_services())
    except KeyboardInterrupt:
        LOGGER.info('Service Stopping...')
    except Exception:
        LOGGER.error(format_exc())
        raise SystemExit(1)
    finally:
        loop.run_until_complete(stop_services())
        loop.stop()
        logging.shutdown()
