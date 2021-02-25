#!/usr/bin/env python3.8
# Released under the MIT License. See LICENSE for details.
#
"""BallisticaCore server manager."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from threading import Lock, Thread, current_thread
from typing import TYPE_CHECKING

# We make use of the bacommon and efro packages as well as site-packages
# included with our bundled Ballistica dist, so we need to add those paths
# before we import them.
sys.path += [
    str(Path(Path(__file__).parent, 'dist', 'ba_data', 'python')),
    str(Path(Path(__file__).parent, 'dist', 'ba_data', 'python-site-packages'))
]

from bacommon.servermanager import ServerConfig, StartServerModeCommand
from efro.dataclasses import dataclass_from_dict, dataclass_validate
from efro.error import CleanError
from efro.terminal import Clr

if TYPE_CHECKING:
    from typing import Optional, List, Dict, Union, Tuple
    from types import FrameType
    from bacommon.servermanager import ServerCommand

VERSION_STR = '1.2'

# Version history:
# 1.2:
#  Added optional --help arg
#  Added --config arg for setting config path and --root for ba_root path
#  Added noninteractive mode and --interactive/--noninteractive to select
# 1.1.1:
#  Switched config reading to use efro.dataclasses.dataclass_from_dict()
# 1.1.0:
#  Added shutdown command
#  Changed restart to default to immediate=True
#  Added clean_exit_minutes, unclean_exit_minutes, and idle_exit_minutes
# 1.0.0:
#  Initial release


class ServerManagerApp:
    """An app which manages BallisticaCore server execution.

    Handles configuring, launching, re-launching, and otherwise
    managing BallisticaCore operating in server mode.
    """

    # How many seconds we wait after asking our subprocess to do an immediate
    # shutdown before bringing down the hammer.
    IMMEDIATE_SHUTDOWN_TIME_LIMIT = 5.0

    def __init__(self) -> None:
        self._config_path = 'config.yaml'
        self._ba_root_path = os.path.abspath('dist/ba_root')
        self._interactive = sys.stdin.isatty()

        # This may override the above defaults.
        self._parse_command_line_args()

        try:
            self._config = self._load_config()
        except Exception as exc:
            raise CleanError(f'Error loading config: {exc}') from exc
        self._wrapper_shutdown_desired = False
        self._done = False
        self._subprocess_commands: List[Union[str, ServerCommand]] = []
        self._subprocess_commands_lock = Lock()
        self._subprocess_force_kill_time: Optional[float] = None
        self._restart_minutes: Optional[float] = None
        self._running = False
        self._subprocess: Optional[subprocess.Popen[bytes]] = None
        self._launch_time = time.time()
        self._subprocess_launch_time: Optional[float] = None
        self._subprocess_sent_auto_restart = False
        self._subprocess_sent_clean_exit = False
        self._subprocess_sent_unclean_exit = False
        self._subprocess_thread: Optional[Thread] = None

        # If we don't have any explicit exit conditions set,
        # we run indefinitely (though we restart our subprocess
        # periodically to clear out leaks/cruft)
        if (self._config.clean_exit_minutes is None
                and self._config.unclean_exit_minutes is None
                and self._config.idle_exit_minutes is None):
            self._restart_minutes = 360.0

    @property
    def config(self) -> ServerConfig:
        """The current config for the app."""
        return self._config

    @config.setter
    def config(self, value: ServerConfig) -> None:
        dataclass_validate(value)
        self._config = value

    @property
    def restart_minutes(self) -> Optional[float]:
        """The time between automatic server restarts.

        Restarting the server periodically can minimize the effect of
        memory leaks or other built-up cruft.
        """
        return self._restart_minutes

    def _prerun(self) -> None:
        """Common code at the start of any run."""

        # Make sure we don't call run multiple times.
        if self._running:
            raise RuntimeError('Already running.')
        self._running = True

        dbgstr = 'debug' if __debug__ else 'opt'
        intstr = 'interactive' if self._interactive else 'noninteractive'
        print(f'{Clr.CYN}{Clr.BLD}BallisticaCore server manager {VERSION_STR}'
              f' starting up ({dbgstr}/{intstr} mode)...{Clr.RST}')

        # Python will handle SIGINT for us (as KeyboardInterrupt) but we
        # need to register a SIGTERM handler so we have a chance to clean
        # up our subprocess when someone tells us to die. (and avoid
        # zombie processes)
        signal.signal(signal.SIGTERM, self._handle_term_signal)

        # Fire off a background thread to wrangle our server binaries.
        self._subprocess_thread = Thread(target=self._bg_thread_main)
        self._subprocess_thread.start()

        # During a run, we make the assumption that cwd is the dir
        # containing this script, so make that so. Up until now that may
        # not be the case (we support being called from any location).
        os.chdir(os.path.abspath(os.path.dirname(__file__)))

    def _postrun(self) -> None:
        """Common code at the end of any run."""
        print(f'{Clr.CYN}Server manager shutting down...{Clr.RST}')

        assert self._subprocess_thread is not None
        if self._subprocess_thread.is_alive():
            print(f'{Clr.CYN}Waiting for subprocess exit...{Clr.RST}')

        # Mark ourselves as shutting down and wait for the process to wrap up.
        self._done = True
        self._subprocess_thread.join()

    def run(self) -> None:
        """Do the thing."""
        if self._interactive:
            self._run_interactive()
        else:
            self._run_noninteractive()

    def _run_noninteractive(self) -> None:
        """Run the app loop to completion noninteractively."""
        self._prerun()
        try:
            while True:
                time.sleep(1.234)
        except KeyboardInterrupt:
            # Gracefully bow out if we kill ourself via keyboard.
            pass
        except SystemExit:
            # We get this from the builtin quit(), our signal handler, etc.
            # Need to catch this so we can clean up, otherwise we'll be
            # left in limbo with our process thread still running.
            pass
        self._postrun()

    def _run_interactive(self) -> None:
        """Run the app loop to completion interactively."""
        import code

        self._prerun()

        # Print basic usage info for interactive mode.
        print(f'{Clr.CYN}Use the "mgr" object to interact with the server.\n'
              f'Type "help(mgr)" for more information.{Clr.RST}')

        context = {'__name__': '__console__', '__doc__': None, 'mgr': self}

        # Enable tab-completion if possible.
        self._enable_tab_completion(context)

        # Now just sit in an interpreter.
        # TODO: make it possible to use IPython if the user has it available.
        try:
            code.interact(local=context, banner='', exitmsg='')
        except SystemExit:
            # We get this from the builtin quit(), our signal handler, etc.
            # Need to catch this so we can clean up, otherwise we'll be
            # left in limbo with our process thread still running.
            pass
        except BaseException as exc:
            print(f'{Clr.SRED}Unexpected interpreter exception:'
                  f' {exc} ({type(exc)}){Clr.RST}')

        self._postrun()

    def cmd(self, statement: str) -> None:
        """Exec a Python command on the current running server subprocess.

        Note that commands are executed asynchronously and no status or
        return value is accessible from this manager app.
        """
        if not isinstance(statement, str):
            raise TypeError(f'Expected a string arg; got {type(statement)}')
        with self._subprocess_commands_lock:
            self._subprocess_commands.append(statement)
        self._block_for_command_completion()

    def _block_for_command_completion(self) -> None:
        # Ideally we'd block here until the command was run so our prompt would
        # print after it's results. We currently don't get any response from
        # the app so the best we can do is block until our bg thread has sent
        # it. In the future we can perhaps add a proper 'command port'
        # interface for proper blocking two way communication.
        while True:
            with self._subprocess_commands_lock:
                if not self._subprocess_commands:
                    break
            time.sleep(0.1)

        # One last short delay so if we come out *just* as the command is sent
        # we'll hopefully still give it enough time to process/print.
        time.sleep(0.1)

    def screenmessage(self,
                      message: str,
                      color: Optional[Tuple[float, float, float]] = None,
                      clients: Optional[List[int]] = None) -> None:
        """Display a screen-message.

        This will have no name attached and not show up in chat history.
        They will show up in replays, however (unless clients is passed).
        """
        from bacommon.servermanager import ScreenMessageCommand
        self._enqueue_server_command(
            ScreenMessageCommand(message=message, color=color,
                                 clients=clients))

    def chatmessage(self,
                    message: str,
                    clients: Optional[List[int]] = None) -> None:
        """Send a chat message from the server.

        This will have the server's name attached and will be logged
        in client chat windows, just like other chat messages.
        """
        from bacommon.servermanager import ChatMessageCommand
        self._enqueue_server_command(
            ChatMessageCommand(message=message, clients=clients))

    def clientlist(self) -> None:
        """Print a list of connected clients."""
        from bacommon.servermanager import ClientListCommand
        self._enqueue_server_command(ClientListCommand())
        self._block_for_command_completion()

    def kick(self, client_id: int, ban_time: Optional[int] = None) -> None:
        """Kick the client with the provided id.

        If ban_time is provided, the client will be banned for that
        length of time in seconds. If it is None, ban duration will
        be determined automatically. Pass 0 or a negative number for no
        ban time.
        """
        from bacommon.servermanager import KickCommand
        self._enqueue_server_command(
            KickCommand(client_id=client_id, ban_time=ban_time))

    def restart(self, immediate: bool = True) -> None:
        """Restart the server subprocess.

        This can be necessary for some config changes to take effect.
        By default, the server will exit immediately. If 'immediate' is passed
        as False, however, the server will instead exit at the next clean
        transition point (end of a series, etc).
        """
        from bacommon.servermanager import ShutdownCommand, ShutdownReason
        self._enqueue_server_command(
            ShutdownCommand(reason=ShutdownReason.RESTARTING,
                            immediate=immediate))

        # If we're asking for an immediate restart but don't get one within
        # the grace period, bring down the hammer.
        if immediate:
            self._subprocess_force_kill_time = (
                time.time() + self.IMMEDIATE_SHUTDOWN_TIME_LIMIT)

    def shutdown(self, immediate: bool = True) -> None:
        """Shut down the server subprocess and exit the wrapper

        By default, the server will exit immediately. If 'immediate' is passed
        as False, however, the server will instead exit at the next clean
        transition point (end of a series, etc).
        """
        from bacommon.servermanager import ShutdownCommand, ShutdownReason
        self._enqueue_server_command(
            ShutdownCommand(reason=ShutdownReason.NONE, immediate=immediate))

        # An explicit shutdown means we know to bail completely once this
        # subprocess completes.
        self._wrapper_shutdown_desired = True

        # If we're asking for an immediate shutdown but don't get one within
        # the grace period, bring down the hammer.
        if immediate:
            self._subprocess_force_kill_time = (
                time.time() + self.IMMEDIATE_SHUTDOWN_TIME_LIMIT)

    def _parse_command_line_args(self) -> None:
        """Parse command line args."""

        i = 1
        argc = len(sys.argv)
        did_set_interactive = False
        while i < argc:
            arg = sys.argv[i]
            if arg == '--help':
                self.print_help()
                sys.exit(0)
            elif arg == '--config':
                if i + 1 >= argc:
                    raise CleanError('Expected a config path as next arg.')
                path = sys.argv[i + 1]
                if not os.path.exists(path):
                    raise CleanError(
                        f"Supplied path does not exist: '{path}'.")
                # We need an abs path because we may be in a different
                # cwd currently than we will be during the run.
                self._config_path = os.path.abspath(path)
                i += 2
            elif arg == '--root':
                if i + 1 >= argc:
                    raise CleanError('Expected a path as next arg.')
                path = sys.argv[i + 1]
                # Unlike config_path, this one doesn't have to exist now.
                # We do however need an abs path because we may be in a
                # different cwd currently than we will be during the run.
                self._ba_root_path = os.path.abspath(path)
                i += 2
            elif arg == '--interactive':
                if did_set_interactive:
                    raise CleanError('interactive/noninteractive can only'
                                     ' be specified once.')
                self._interactive = True
                did_set_interactive = True
                i += 1
            elif arg == '--noninteractive':
                if did_set_interactive:
                    raise CleanError('interactive/noninteractive can only'
                                     ' be specified once.')
                self._interactive = False
                did_set_interactive = True
                i += 1
            else:
                raise CleanError(f"Invalid arg: '{arg}'.")

    @classmethod
    def _par(cls, txt: str) -> str:
        import textwrap
        ind = ' ' * 2
        out = textwrap.fill(txt, 80, initial_indent=ind, subsequent_indent=ind)
        return f'{out}\n'

    @classmethod
    def print_help(cls) -> None:
        """Print app help."""
        filename = os.path.basename(__file__)
        out = (
            f'{Clr.BLD}{filename} usage:{Clr.RST}\n' + cls._par(
                'This script handles configuring, launching, re-launching,'
                ' and otherwise managing BallisticaCore operating'
                ' in server mode. It can be run with no arguments, but'
                ' accepts the following optional ones:') + f'\n'
            f'{Clr.BLD}--help:{Clr.RST}\n'
            f'  Show this help.\n'
            f'\n'
            f'{Clr.BLD}--config [path]{Clr.RST}\n' + cls._par(
                'Set the config file read by the server script. This should'
                ' be in yaml format. Note that yaml is backwards compatible'
                ' with json so you can just write json if you want to. If'
                ' not specified, the script will look for a file named'
                ' \'config.yaml\' in the same directory as the script.') + '\n'
            f'{Clr.BLD}--root [path]{Clr.RST}\n' + cls._par(
                'Set the ballistica root directory. This is where the game'
                ' will read and write its caches, state files, downloaded'
                ' assets, etc. It needs to be a writable directory. If not'
                ' specified, the script will use the \'dist/ba_root\''
                ' directory relative to itself.') + '\n'
            f'{Clr.BLD}--interactive{Clr.RST}\n'
            f'{Clr.BLD}--noninteractive{Clr.RST}\n' + cls._par(
                'Specify whether the script should run interactively.'
                ' In interactive mode, the script creates a Python interpreter'
                ' and reads commands from stdin, allowing for live interaction'
                ' with the server. The server script will then exit when '
                'end-of-file is reached in stdin. Noninteractive mode creates'
                ' no interpreter and is more suited to being run in automated'
                ' scenarios. By default, interactive mode will be used if'
                ' a terminal is detected and noninteractive mode otherwise.'))
        print(out)

    def _load_config(self) -> ServerConfig:

        if os.path.exists(self._config_path):
            import yaml
            with open(self._config_path) as infile:
                user_config_raw = yaml.safe_load(infile.read())

            # An empty config file will yield None, and that's ok.
            if user_config_raw is not None:
                return dataclass_from_dict(ServerConfig, user_config_raw)
        else:
            print(
                f"Warning: config file not found at '{self._config_path}'"
                f'; will use default config.',
                file=sys.stderr,
                flush=True)

        # Go with defaults if we weren't able to load anything.
        return ServerConfig()

    def _enable_tab_completion(self, locs: Dict) -> None:
        """Enable tab-completion on platforms where available (linux/mac)."""
        try:
            import readline
            import rlcompleter
            readline.set_completer(rlcompleter.Completer(locs).complete)
            readline.parse_and_bind('tab:complete')
        except ImportError:
            # This is expected (readline doesn't exist under windows).
            pass

    def _bg_thread_main(self) -> None:
        """Top level method run by our bg thread."""
        while not self._done:
            self._run_server_cycle()

    def _handle_term_signal(self, sig: int, frame: FrameType) -> None:
        """Handle signals (will always run in the main thread)."""
        del sig, frame  # Unused.
        raise SystemExit()

    def _run_server_cycle(self) -> None:
        """Spin up the server subprocess and run it until exit."""

        self._prep_subprocess_environment()

        # Launch the binary and grab its stdin;
        # we'll use this to feed it commands.
        self._subprocess_launch_time = time.time()

        # Set an environment var so the server process knows its being
        # run under us. This causes it to ignore ctrl-c presses and other
        # slight behavior tweaks. Hmm; should this be an argument instead?
        os.environ['BA_SERVER_WRAPPER_MANAGED'] = '1'

        print(f'{Clr.CYN}Launching server subprocess...{Clr.RST}')
        binary_name = ('ballisticacore_headless.exe'
                       if os.name == 'nt' else './ballisticacore_headless')
        assert self._ba_root_path is not None
        self._subprocess = subprocess.Popen(
            [binary_name, '-cfgdir', self._ba_root_path],
            stdin=subprocess.PIPE,
            cwd='dist')

        # Do the thing.
        # No matter how this ends up, make sure the process is dead after.
        try:
            self._run_subprocess_until_exit()
        finally:
            self._kill_subprocess()

        # If we want to die completely after this subprocess has ended,
        # tell the main thread to die.
        if self._wrapper_shutdown_desired:

            # Only do this if the main thread is not already waiting for
            # us to die; otherwise it can lead to deadlock.
            if not self._done:
                self._done = True

                # This should break the main thread out of its blocking
                # interpreter call.
                os.kill(os.getpid(), signal.SIGTERM)

    def _prep_subprocess_environment(self) -> None:
        """Write files that must exist at process launch."""

        assert self._ba_root_path is not None
        os.makedirs(self._ba_root_path, exist_ok=True)
        cfgpath = os.path.join(self._ba_root_path, 'config.json')
        if os.path.exists(cfgpath):
            with open(cfgpath) as infile:
                bincfg = json.loads(infile.read())
        else:
            bincfg = {}

        # Some of our config values translate directly into the
        # ballisticacore config file; the rest we pass at runtime.
        bincfg['Port'] = self._config.port
        bincfg['Auto Balance Teams'] = self._config.auto_balance_teams
        bincfg['Show Tutorial'] = False
        bincfg['Idle Exit Minutes'] = self._config.idle_exit_minutes
        with open(cfgpath, 'w') as outfile:
            outfile.write(json.dumps(bincfg))

    def _enqueue_server_command(self, command: ServerCommand) -> None:
        """Enqueue a command to be sent to the server.

        Can be called from any thread.
        """
        with self._subprocess_commands_lock:
            self._subprocess_commands.append(command)

    def _send_server_command(self, command: ServerCommand) -> None:
        """Send a command to the server.

        Must be called from the server process thread.
        """
        import pickle
        assert current_thread() is self._subprocess_thread
        assert self._subprocess is not None
        assert self._subprocess.stdin is not None
        val = repr(pickle.dumps(command))
        assert '\n' not in val
        execcode = (f'import ba._servermode;'
                    f' ba._servermode._cmd({val})\n').encode()
        self._subprocess.stdin.write(execcode)
        self._subprocess.stdin.flush()

    def _run_subprocess_until_exit(self) -> None:
        assert current_thread() is self._subprocess_thread
        assert self._subprocess is not None
        assert self._subprocess.stdin is not None

        # Send the initial server config which should kick things off.
        # (but make sure its values are still valid first)
        dataclass_validate(self._config)
        self._send_server_command(StartServerModeCommand(self._config))

        while True:

            # If the app is trying to shut down, nope out immediately.
            if self._done:
                break

            # Pass along any commands to our process.
            with self._subprocess_commands_lock:
                for incmd in self._subprocess_commands:
                    # If we're passing a raw string to exec, no need to wrap it
                    # in any proper structure.
                    if isinstance(incmd, str):
                        self._subprocess.stdin.write((incmd + '\n').encode())
                        self._subprocess.stdin.flush()
                    else:
                        self._send_server_command(incmd)
                self._subprocess_commands = []

            # Request restarts/shut-downs for various reasons.
            self._request_shutdowns_or_restarts()

            # If they want to force-kill our subprocess, simply exit this
            # loop; the cleanup code will kill the process.
            if (self._subprocess_force_kill_time is not None
                    and time.time() > self._subprocess_force_kill_time):
                print(f'{Clr.CYN}Force-killing subprocess...{Clr.RST}')
                break

            # Watch for the process exiting on its own..
            code: Optional[int] = self._subprocess.poll()
            if code is not None:
                if code == 0:
                    clr = Clr.CYN
                    slp = 0.0
                    desc = ''
                elif code == 154:
                    clr = Clr.CYN
                    slp = 0.0
                    desc = ' (idle_exit_minutes reached)'
                    self._wrapper_shutdown_desired = True
                else:
                    clr = Clr.SRED
                    slp = 5.0  # Avoid super fast death loops.
                    desc = ''
                print(f'{clr}Server subprocess exited'
                      f' with code {code}{desc}.{Clr.RST}')
                self._reset_subprocess_vars()
                time.sleep(slp)
                break

            time.sleep(0.25)

    def _request_shutdowns_or_restarts(self) -> None:
        assert current_thread() is self._subprocess_thread
        assert self._subprocess_launch_time is not None
        sincelaunch = time.time() - self._subprocess_launch_time

        if (self._restart_minutes is not None and sincelaunch >
            (self._restart_minutes * 60.0)
                and not self._subprocess_sent_auto_restart):
            print(f'{Clr.CYN}restart_minutes ({self._restart_minutes})'
                  f' elapsed; requesting subprocess'
                  f' soft restart...{Clr.RST}')
            self.restart()
            self._subprocess_sent_auto_restart = True

        if self._config.clean_exit_minutes is not None:
            elapsed = (time.time() - self._launch_time) / 60.0
            if (elapsed > self._config.clean_exit_minutes
                    and not self._subprocess_sent_clean_exit):
                print(f'{Clr.CYN}clean_exit_minutes'
                      f' ({self._config.clean_exit_minutes})'
                      f' elapsed; requesting subprocess'
                      f' shutdown...{Clr.RST}')
                self.shutdown(immediate=False)
                self._subprocess_sent_clean_exit = True

        if self._config.unclean_exit_minutes is not None:
            elapsed = (time.time() - self._launch_time) / 60.0
            if (elapsed > self._config.unclean_exit_minutes
                    and not self._subprocess_sent_unclean_exit):
                print(f'{Clr.CYN}unclean_exit_minutes'
                      f' ({self._config.unclean_exit_minutes})'
                      f' elapsed; requesting subprocess'
                      f' shutdown...{Clr.RST}')
                self.shutdown(immediate=True)
                self._subprocess_sent_unclean_exit = True

    def _reset_subprocess_vars(self) -> None:
        self._subprocess = None
        self._subprocess_launch_time = None
        self._subprocess_sent_auto_restart = False
        self._subprocess_sent_clean_exit = False
        self._subprocess_sent_unclean_exit = False
        self._subprocess_force_kill_time = None

    def _kill_subprocess(self) -> None:
        """End the server subprocess if it still exists."""
        assert current_thread() is self._subprocess_thread
        if self._subprocess is None:
            return

        print(f'{Clr.CYN}Stopping subprocess...{Clr.RST}')

        # First, ask it nicely to die and give it a moment.
        # If that doesn't work, bring down the hammer.
        self._subprocess.terminate()
        try:
            self._subprocess.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._subprocess.kill()
        self._reset_subprocess_vars()
        print(f'{Clr.CYN}Subprocess stopped.{Clr.RST}')


def main() -> None:
    """Run the BallisticaCore server manager."""
    try:
        ServerManagerApp().run()
    except CleanError as exc:
        # For clean errors, do a simple print and fail; no tracebacks/etc.
        # Any others will bubble up and give us the usual mess.
        exc.pretty_print()
        sys.exit(1)


if __name__ == '__main__':
    main()
