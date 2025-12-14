from __future__ import annotations

# import an additional thing for proper PyInstaller freeze support
from multiprocessing import freeze_support


if __name__ == "__main__":
    freeze_support()
    import io
    import os
    import sys
    import signal
    import asyncio
    import logging
    import argparse
    import warnings
    import tempfile
    import traceback
    import subprocess
    from PySide6.QtWidgets import QApplication, QMessageBox
    from pathlib import Path
    from typing import NoReturn, TYPE_CHECKING

    import truststore
    truststore.inject_into_ssl()

    from translate import _
    from twitch import Twitch
    from settings import Settings
    from version import __version__
    from exceptions import CaptchaRequired
    from utils import lock_file
    from constants import LOGGING_LEVELS, SELF_PATH, FILE_FORMATTER, LOG_PATH, LOCK_PATH

    if TYPE_CHECKING:
        from _typeshed import SupportsWrite

    warnings.simplefilter("default", ResourceWarning)

    # import tracemalloc
    # tracemalloc.start(3)

    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10 or higher is required")

    class Parser(argparse.ArgumentParser):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self._message: io.StringIO = io.StringIO()

        def _print_message(self, message: str, file: SupportsWrite[str] | None = None) -> None:
            self._message.write(message)
            # print(message, file=self._message)

        def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
            try:
                super().exit(status, message)  # sys.exit(2)
            finally:
                _show_message(
                    "Argument Parser Error" if status else "Twitch Drops Miner",
                    self._message.getvalue(),
                    error=bool(status),
                )

    class ParsedArgs(argparse.Namespace):
        _verbose: int
        _debug_ws: bool
        _debug_gql: bool
        log: bool
        tray: bool
        dump: bool

        # TODO: replace int with union of literal values once typeshed updates
        @property
        def logging_level(self) -> int:
            return LOGGING_LEVELS[min(self._verbose, 4)]

        @property
        def debug_ws(self) -> int:
            """
            If the debug flag is True, return DEBUG.
            If the main logging level is DEBUG, return INFO to avoid seeing raw messages.
            Otherwise, return NOTSET to inherit the global logging level.
            """
            if self._debug_ws:
                return logging.DEBUG
            elif self._verbose >= 4:
                return logging.INFO
            return logging.NOTSET

        @property
        def debug_gql(self) -> int:
            if self._debug_gql:
                return logging.DEBUG
            elif self._verbose >= 4:
                return logging.INFO
            return logging.NOTSET

    def _ensure_qapp() -> QApplication:
        app = QApplication.instance()
        if app is None:
            app = QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False)
        return app

    def _show_message(title: str, text: str, *, error: bool) -> None:
        _ensure_qapp()
        if error:
            QMessageBox.critical(None, title, text)
        else:
            QMessageBox.information(None, title, text)

    # handle input parameters
    # NOTE: parser output is shown via message box
    _ensure_qapp()
    parser = Parser(
        SELF_PATH.name,
        description="A program that allows you to mine timed drops on Twitch.",
    )
    parser.add_argument("--version", action="version", version=f"v{__version__}")
    parser.add_argument("-v", dest="_verbose", action="count", default=0)
    parser.add_argument("--tray", action="store_true")
    parser.add_argument("--log", action="store_true")
    parser.add_argument("--dump", action="store_true")
    # undocumented debug args
    parser.add_argument(
        "--debug-ws", dest="_debug_ws", action="store_true", help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--debug-gql", dest="_debug_gql", action="store_true", help=argparse.SUPPRESS
    )
    args = parser.parse_args(namespace=ParsedArgs())
    # load settings
    try:
        settings = Settings(args)
    except Exception:
        _show_message(
            "Settings error",
            f"There was an error while loading the settings file:\n\n{traceback.format_exc()}",
            error=True,
        )
        sys.exit(4)
    # get rid of unneeded objects
    del parser

    def find_venv_python() -> str | None:
        """Find Python executable in common venv locations"""
        script_dir = SELF_PATH.parent
        common_venv_names = ['.venv', 'venv', 'env', '.env', 'virtualenv']
        
        for venv_name in common_venv_names:
            venv_path = script_dir / venv_name
            if venv_path.exists() and venv_path.is_dir():
                # Check for Python executable
                if sys.platform == "win32":
                    python_exe = venv_path / "Scripts" / "python.exe"
                else:
                    python_exe = venv_path / "bin" / "python"
                
                if python_exe.exists():
                    return str(python_exe)
        
        return None

    def create_restart_script() -> Path:
        """Create a temporary script to restart the application"""
        # Determine if we're running as exe or python script
        if getattr(sys, 'frozen', False):
            # Running as compiled exe
            app_path = sys.executable
            is_exe = True
            python_exe = None
        else:
            # Running as Python script
            app_path = SELF_PATH
            is_exe = False
            # Try to find venv Python, fall back to current Python
            python_exe = find_venv_python() or sys.executable
        
        # Get the original command line arguments
        args = ' '.join(f'"{arg}"' if ' ' in arg else arg for arg in sys.argv[1:])
        
        if sys.platform == "win32":
            # Create a temporary batch file for Windows
            script_content = f"""@echo off
timeout /t 2 /nobreak >nul
"""
            if is_exe:
                script_content += f'start "" "{app_path}" {args}\n'
            else:
                script_content += f'start "" "{python_exe}" "{app_path}" {args}\n'
            
            script_content += "del /f /q \"%~f0\"\n"
            
            fd, script_path = tempfile.mkstemp(suffix='.bat', text=True)
            with os.fdopen(fd, 'w') as f:
                f.write(script_content)
        else:
            # Create a temporary shell script for Linux/Mac
            script_content = f"""#!/bin/bash
sleep 2
"""
            if is_exe:
                script_content += f'"{app_path}" {args} &\n'
            else:
                script_content += f'"{python_exe}" "{app_path}" {args} &\n'
            
            script_content += f'rm -f "$0"\n'
            
            fd, script_path = tempfile.mkstemp(suffix='.sh', text=True)
            with os.fdopen(fd, 'w') as f:
                f.write(script_content)
            os.chmod(script_path, 0o755)
        
        return Path(script_path)

    def trigger_full_restart():
        """Trigger a full application restart by launching a restart script"""
        try:
            script_path = create_restart_script()
            
            if sys.platform == "win32":
                # Launch the batch file detached on Windows
                # Use STARTUPINFO to hide the window
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE
                
                proc = subprocess.Popen(
                    [str(script_path)],
                    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                    close_fds=True,
                    startupinfo=startupinfo,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
            else:
                # Launch the shell script detached on Linux/Mac
                proc = subprocess.Popen(
                    [str(script_path)],
                    start_new_session=True,
                    close_fds=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
            
            # Don't wait for the process - it's meant to outlive this one
            # This prevents the ResourceWarning
            proc._handle = None  # type: ignore
            
            return True
        except Exception as e:
            print(f"Failed to create restart script: {e}")
            return False

    # client run
    async def main():
        import aiohttp
        
        # set language
        try:
            _.set_language(settings.language)
        except ValueError:
            # this language doesn't exist - stick to English
            pass

        # handle logging stuff
        if settings.logging_level > logging.DEBUG:
            # redirect the root logger into a NullHandler, effectively ignoring all logging calls
            # that aren't ours. This always runs, unless the main logging level is DEBUG or lower.
            logging.getLogger().addHandler(logging.NullHandler())
        logger = logging.getLogger("TwitchDrops")
        logger.setLevel(settings.logging_level)
        if settings.log:
            handler = logging.FileHandler(LOG_PATH)
            handler.setFormatter(FILE_FORMATTER)
            logger.addHandler(handler)
        logging.getLogger("TwitchDrops.gql").setLevel(settings.debug_gql)
        logging.getLogger("TwitchDrops.websocket").setLevel(settings.debug_ws)

        max_restart_attempts = 0  # 0 means infinite
        restart_count = 0
        
        while True:
            exit_status = 0
            client = Twitch(settings)
            loop = asyncio.get_running_loop()
            if sys.platform == "linux":
                loop.add_signal_handler(signal.SIGINT, lambda *_: client.gui.close())
                loop.add_signal_handler(signal.SIGTERM, lambda *_: client.gui.close())
            try:
                await client.run()
            except CaptchaRequired:
                exit_status = 1
                client.prevent_close()
                client.print(_("error", "captcha"))
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError, aiohttp.ClientPayloadError) as e:
                exit_status = 1
                if settings.auto_restart_on_error and not client.gui.close_requested:
                    restart_count += 1
                    client.print(f"Connection error encountered: {type(e).__name__}\n")
                    if max_restart_attempts > 0 and restart_count >= max_restart_attempts:
                        client.prevent_close()
                        client.print(f"Maximum restart attempts ({max_restart_attempts}) reached. Stopping.\n")
                    else:
                        client.print(f"Auto-restart enabled. Triggering full application restart... (Attempt {restart_count})\n")
                        # Save state before restart
                        client.save(force=True)
                        # Trigger full restart
                        if trigger_full_restart():
                            # Exit this instance so the new one can start
                            break
                        else:
                            client.print("Failed to trigger restart. Application will terminate.\n")
                            client.prevent_close()
                else:
                    client.prevent_close()
                    client.print(f"Connection error encountered:\n{traceback.format_exc()}")
            except Exception:
                exit_status = 1
                client.prevent_close()
                client.print("Fatal error encountered:\n")
                client.print(traceback.format_exc())
            finally:
                if sys.platform == "linux":
                    loop.remove_signal_handler(signal.SIGINT)
                    loop.remove_signal_handler(signal.SIGTERM)
                client.print(_("gui", "status", "exiting"))
                await client.shutdown()
            
            # Normal shutdown procedure
            if not client.gui.close_requested:
                # user didn't request the closure
                client.gui.tray.change_icon("error")
                client.print(_("status", "terminated"))
                client.gui.status.update(_("gui", "status", "terminated"))
                # notify the user about the closure
                client.gui.grab_attention(sound=True)
            await client.gui.wait_until_closed()
            # save the application state
            # NOTE: we have to do it after wait_until_closed,
            # because the user can alter some settings between app termination and closing the window
            client.save(force=True)
            client.gui.stop()
            client.gui.close_window()
            sys.exit(exit_status)

    try:
        # use lock_file to check if we're not already running
        success, file = lock_file(LOCK_PATH)
        if not success:
            # already running - exit
            sys.exit(3)

        asyncio.run(main())
    finally:
        file.close()
