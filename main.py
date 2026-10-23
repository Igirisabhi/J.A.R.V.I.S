import os
import sys
import signal
import ctypes
import atexit
import argparse
import importlib.metadata
import threading

# Ignore Ctrl+C / SIGINT across all threads during assistant operation
try:
    signal.signal(signal.SIGINT, lambda s, f: None)
except Exception:
    pass

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        pass

if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        pass

JARVIS_PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis.pid")
_jarvis_instance_mutex = None


def _configure_background_output():
    """Keep diagnostics available when launched without a Windows console."""
    if sys.stdout is None or sys.stderr is None:
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime", "jarvis.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        log_stream = open(log_path, "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = log_stream
        if sys.stderr is None:
            sys.stderr = log_stream


def enforce_single_instance():
    """Ensure ONLY ONE Jarvis core instance runs per user session."""
    global _jarvis_instance_mutex
    current_pid = os.getpid()
    kernel32 = ctypes.windll.kernel32

    # The mutex is the authoritative lock. Never kill a process just because
    # its PID happens to match a stale PID file; Windows can reuse PIDs.
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.GetLastError.restype = ctypes.c_ulong
    kernel32.SetLastError.argtypes = [ctypes.c_ulong]
    kernel32.SetLastError.restype = None
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    kernel32.SetLastError(0)
    _jarvis_instance_mutex = kernel32.CreateMutexW(
        None, False, "JarvisCore_SingleInstance_Session_Mutex"
    )
    if not _jarvis_instance_mutex:
        raise OSError("Could not create the Jarvis single-instance mutex.")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(_jarvis_instance_mutex)
        _jarvis_instance_mutex = None
        return False

    try:
        with open(JARVIS_PID_FILE, "w") as f:
            f.write(str(current_pid))
    except OSError:
        pass

    return True


def _remove_own_pid_file():
    try:
        with open(JARVIS_PID_FILE, "r", encoding="ascii") as f:
            recorded_pid = int(f.read().strip())
        if recorded_pid == os.getpid():
            os.remove(JARVIS_PID_FILE)
    except (OSError, ValueError):
        pass


def run_text_mode(debug=False):
    """Run local command parsing and execution without microphone or speech."""
    from core.command_engine import parse_action, parse_command_chain
    from core.error_log import log_error
    from tools.system_tools import handle_command

    print("Jarvis text mode. Type 'sleep' to exit.")
    from core.events import event_bus
    event_bus.publish("state_changed", state="LISTENING")
    while True:
        try:
            command = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if command.lower() == "sleep":
            return
        if not command:
            continue
        from core.command_engine import normalize_voice_text
        event_bus.publish("heard", text=command, normalized=normalize_voice_text(command))

        try:
            if debug:
                from core.command_engine import normalize_voice_text
                print(f"[DEBUG] Heard: {command}")
                print(f"[DEBUG] Normalized: {normalize_voice_text(command)}")
                actions = parse_command_chain(command)
                if actions:
                    parsed = actions
                else:
                    action = parse_action(command)
                    parsed = [action] if action else []
                print(f"[DEBUG] Parsed: {parsed}")
            result = handle_command(command)
        except Exception as error:
            log_error("Text command failed", error)
            result = "I couldn't process that command."
        if debug:
            print(f"[DEBUG] Executed: {result}")
        print(f"Jarvis: {result or 'I could not match a local command.'}")
        event_bus.publish("state_changed", state="SPEAKING")
        event_bus.publish("step_result", success=True, message=result or "No local command matched.")
        event_bus.publish("state_changed", state="IDLE")


def check_runtime_compatibility():
    """Report the supported Python/PyAV range without blocking startup."""
    if not (3, 10) <= sys.version_info[:2] <= (3, 12):
        print(f"WARNING: Jarvis supports Python 3.10-3.12; running {sys.version.split()[0]}.")
    try:
        version = importlib.metadata.version("av")
        major = int(version.split(".", 1)[0])
        if major >= 19:
            print(f"WARNING: PyAV {version} is installed; faster-whisper requires PyAV below 19.")
    except (importlib.metadata.PackageNotFoundError, ValueError):
        pass


def main():
    _configure_background_output()
    parser = argparse.ArgumentParser(description="Jarvis voice assistant")
    parser.add_argument("--text", action="store_true", help="Use text input instead of the microphone")
    parser.add_argument("--debug", action="store_true", help="Print heard, parsed, and executed commands")
    parser.add_argument("--diagnose", action="store_true", help="Run non-destructive installation and device diagnostics")
    ui_group = parser.add_mutually_exclusive_group()
    ui_group.add_argument("--ui", action="store_true", help="Show the optional Jarvis HUD")
    ui_group.add_argument("--no-ui", action="store_true", help="Run without the optional Jarvis HUD (default)")
    args = parser.parse_args()

    check_runtime_compatibility()

    if args.diagnose:
        from core.diagnostics import diagnostics_exit_code, format_diagnostics, run_diagnostics
        results = run_diagnostics()
        print(format_diagnostics(results))
        raise SystemExit(diagnostics_exit_code(results))

    if not args.text and not enforce_single_instance():
        print("Jarvis is already running. Close the existing instance before starting another UI/backend pair.")
        raise SystemExit(0)
    if not args.text:
        atexit.register(_remove_own_pid_file)

    if args.ui:
        try:
            from ui.main import run_ui
        except ImportError as error:
            print(f"The optional UI could not start: {error}")
            raise SystemExit(2) from error

        def run_assistant():
            try:
                if args.text:
                    run_text_mode(debug=args.debug)
                else:
                    from core.events import event_bus
                    event_bus.publish("state_changed", state="STARTING", detail="Starting microphone listener")
                    from voice.wake_word import main as voice_main
                    voice_main(debug=args.debug)
            except Exception as error:
                import traceback
                try:
                    from core.error_log import log_error
                    log_error("Voice backend startup failed", error)
                    from core.events import event_bus
                    event_bus.publish("error", message=f"Voice backend failed to start: {error}")
                    event_bus.publish("state_changed", state="ERROR", detail="Voice backend failed to start")
                except Exception:
                    pass
                traceback.print_exc()

        run_ui(debug=args.debug, backend_start=lambda: threading.Thread(
            target=run_assistant, name="JarvisCore", daemon=True
        ).start())
        return

    if args.text:
        run_text_mode(debug=args.debug)
        return

    from voice.wake_word import main as voice_main
    voice_main(debug=args.debug)


if __name__ == "__main__":
    main()
