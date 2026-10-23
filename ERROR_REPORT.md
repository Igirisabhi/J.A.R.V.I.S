# Jarvis Audit Report

## ENVIRONMENT

- OS: Microsoft Windows 10.0.26200 (Windows 11), Python 3.12.14.
- UI: PySide6 6.12.0 with Qt WebEngine.
- Speech recognition: Groq cloud transcription (`whisper-large-v3-turbo`) through `voice/stt_groq.py`; Groq package 1.7.0. Offline transcription dependency: faster-whisper 1.2.1.
- Text-to-speech: Groq cloud TTS through `voice/tts.py`; Groq package 1.7.0. A separate Kokoro worker uses `.ttsenv`.
- Start with `start_jarvis.bat` or `.venv\Scripts\python.exe main.py --ui`.
- The active `.venv` was checked. A clean dependency installation was not performed; no dependency files were changed.

## ERRORS FOUND

### 1. App-scoped search could route to the browser

- File: `core/command_engine.py:386-402, 608-612, 844-894`; `tests/test_runtime_regressions.py:108-145`
- Severity: wrong behavior
- Exact pre-fix error: No exception was emitted; the reported failure was that “search for something in app” searched in Chrome instead of the active app.
- Trigger: `search for voice in app` or `search in app for voice`.
- Root cause: The parser did not consistently preserve the current-app destination, and destination resolution could fall through to a browser search.
- Status: FIXED
- Changed lines: The parser tags both phrasings as `this app`; execution resolves the active native app and returns a clear failure if it cannot identify or open the target.
- Verification: `test_both_current_app_search_phrasings_are_scoped`, `test_current_app_search_uses_the_active_native_app`, and `test_current_app_search_fails_clearly_when_no_app_is_known` pass.

### 2. “Play first video” could act on unrelated media

- File: `core/command_engine.py:1495-1502`; `tests/test_runtime_regressions.py:41-60, 176-191`
- Severity: wrong behavior
- Exact pre-fix error: No exception was emitted; the reported behavior was that the requested first YouTube result did not reliably play.
- Trigger: `play first video`, especially without a recorded YouTube search result.
- Root cause: The action could reach generic media controls without a resolved result from a YouTube search.
- Status: FIXED
- Changed lines: Playback now requires a result resolved from a YouTube search and reports when no result can be fetched; it does not send a play key to arbitrary current media.
- Verification: `test_play_first_youtube_result_opens_the_resolved_search_result`, `test_first_video_without_a_youtube_search_does_not_play_random_media`, and `test_first_video_with_no_fetchable_search_result_reports_failure` pass.

### 3. Opening a file in the active Explorer folder unnecessarily required a workspace

- File: `core/command_engine.py` (file-open path resolution); `tests/test_runtime_regressions.py:146-157`
- Severity: wrong behavior
- Exact pre-fix error: No exception was emitted; the command returned the workspace-required response even when the file was in the open Explorer folder.
- Trigger: Open a file in an already-open Explorer folder before setting a workspace.
- Root cause: File opening used the workspace requirement intended for file modifications.
- Status: FIXED
- Changed lines: Read-only file opening can use the active Explorer folder while modification commands still require a workspace.
- Verification: `test_open_file_in_open_explorer_folder_does_not_require_workspace` passes.

### 4. Opening an app referenced a missing app-name constant

- File: `core/command_engine.py:245-247, 608-612, 1375-1378`
- Severity: crash
- Exact pre-fix traceback: Not captured. The module did not define the referenced `KNOWN_APPS` name.
- Trigger: An app-open command that reached app matching.
- Root cause: The command engine imported a constant that was not provided by `tools.window_tools`.
- Status: FIXED
- Changed lines: App matching now uses `APP_OPEN_COMMANDS`, `APP_PROCESSES`, and `canonical_app_name` from `tools.window_tools`.
- Verification: `test_opening_an_app_does_not_import_a_missing_app_constant` passes.

### 5. Configured headset microphone could not be opened on this host

- File: `core/audio_devices.py`; `voice/recorder.py`; `voice/wake_word.py`; `voice/thinking_listener.py`; `voice/tts.py`; `voice/mic_test.py`
- Severity: wrong behavior
- Exact output from the approved short microphone check:

```text
🎙️ Microphone: Headset (@System32\drivers\bthhfenum.sys,#2;%1 Hands-Free%0
;(CMF Headphone Pro)) (device 32)
⚠️ Could not open ... CMF Headphone Pro ... trying another input: Error starting stream: Unanticipated host error [PaErrorCode -9999]: 'WdmSyncIoctl: DeviceIoControl GLE = 0x0000048F (prop_set = {1D58C920-AC9B-11CF-A5D6-28DB04C10000}, prop_id = 0)' [Windows WDM-KS error 0]
⚠️ Microphone unavailable: Error opening InputStream: Unanticipated host error [PaErrorCode -9999]: 'Undefined external error.' [MME error 1]
MIC_CHECK: no speech captured; STT was not called
```

- Trigger: Capture a short command from the configured `CMF Headphone Pro` input.
- Root cause: The selected Windows audio endpoints returned PortAudio/WDM-KS and MME stream errors. The check therefore captured no speech and did not contact STT.
- Status: CANNOT VERIFY
- Changed lines: Added configured-device matching and retry across matching input endpoints; prevented an unrelated headset with a similar name from being treated as the configured device; converted final stream failure into a reported recording failure.
- What is needed: Make the configured headset input available to Windows, then rerun `voice/mic_test.py`. No speech-provider request was made during this check.

### 6. HUD failed to load and could not pin to the wallpaper layer in this run

- File: `ui/main.py` (HUD creation, loading, and desktop attachment)
- Severity: wrong behavior
- Exact output from `main.py --ui --text --debug`:

```text
[UI] Desktop pinning failed; HUD remains visible in the normal window layer.
requestActivate() called for  QWidgetWindow(... name="QWebEngineViewClassWindow")  which has Qt::WindowDoesNotAcceptFocus set.
[UI] Reference HUD failed to load.
```

- Trigger: Start the HUD in this execution environment.
- Root cause: The HUD’s desktop attachment did not find a usable wallpaper host, and Qt WebEngine did not report a successful page load. The app window was not exposed by the desktop inspection tool, so its appearance could not be checked.
- Status: CANNOT VERIFY
- Changed lines: Kept the HUD at its configured screen geometry without calling `showFullScreen()`; retained the non-activating fallback behavior.
- What is needed: Verify on the normal interactive Windows desktop. This environment did not provide a targetable Jarvis window or a reliable visual screenshot, so no further UI behavior is claimed as verified.

### 7. The existing Kokoro environment cannot start its Python interpreter

- File: `.ttsenv` (environment only; not modified); `setup.bat`
- Severity: crash
- Exact output:

```text
Unable to create process using '"C:\Users\brabh\AppData\Local\Programs\Python\Python312\python.exe" --version'
```

- Trigger: `.ttsenv\Scripts\python.exe --version` or a Kokoro worker launch.
- Root cause: The existing virtual environment points to a Python executable that this host cannot run.
- Status: CANNOT VERIFY
- Changed lines: `setup.bat` now explains that an unusable existing `.ttsenv` is left untouched and must be rebuilt after a working Python 3.12 installation is available.
- What is needed: Install/repair Python 3.12 and recreate the Kokoro environment. The existing environment was not deleted or modified.

### 8. The repository lint pass reports existing diagnostics

- File: Repository-wide; no Ruff configuration is present.
- Severity: minor
- Exact output summary from the Ruff check:

```text
Found 356 errors.
[*] 97 fixable with `--fix` (12 unsafe fixes available with `--fix --unsafe-fixes`).
```

- Trigger: `C:\Users\brabh\AppData\Local\Temp\jarvis-audit-tools\Scripts\ruff.exe check --isolated .`
- Root cause: The repository has no Ruff rule configuration, and the current whole-project pass reports broad exception handling, silent exception handling, import ordering, and style findings across existing modules.
- Status: OPEN
- What is needed: Agree on the project’s intended Ruff rule set before applying a repository-wide lint cleanup. The targeted undefined-name/import check passed; this does not clear the whole-project lint backlog.

### 9. Generic “maximize” could target Jarvis’s stale remembered app

- File: `core/command_engine.py:1044-1047`; `tests/test_runtime_regressions.py:26-39`
- Severity: wrong behavior
- Exact output from the reported occurrence:

```text
🛠️ Tool: Maximized Quick settings.
```

- Trigger: `maximize` while Jarvis’s remembered app is stale and a system popup is foreground.
- Root cause: The command executor sent the remembered app name directly to the app-specific maximizer instead of using the helper that inspects the active window and filters system popups.
- Status: FIXED
- Changed lines: Generic maximize now calls `maximize_current_window()`, which checks the foreground window and uses the remembered app only as a fallback.
- Verification: `test_generic_maximize_uses_the_active_window_not_a_stale_app` passes.

## THINGS THAT WORK

- `.venv\Scripts\python.exe -m compileall -q main.py core tools voice ui tests tts_worker.py` — exit code 0.
- `.venv\Scripts\python.exe -m unittest discover -v tests` — 29 tests passed.
- `.venv\Scripts\python.exe -m pip check` — `No broken requirements found.`
- Temporary Ruff undefined-name/import check, `ruff check --isolated --select F .` — `All checks passed!`
- Temporary mypy check on `main.py core tools voice ui` — `Success: no issues found in 32 source files`.
- `main.py --text --debug` handled `what time is it` and an unrecognized command, printed the response, and exited on `quit`.
- Importing `voice.wake_word` loaded the bundled ONNX wake-word model successfully.
- Unit tests cover workspace path traversal rejection, symlink escape rejection, command chaining, and app-scoped search routing.

## NOT VERIFIED

- A real command could not be recorded from the configured microphone; STT was not called.
- Cloud STT/TTS requests were not made; API credentials were not inspected.
- Kokoro model loading was not verified because `.ttsenv` cannot launch its interpreter.
- The HUD could not be visually inspected at mobile/tablet/desktop sizes; this is a Windows desktop app, and no targetable Jarvis window was returned.
- Camera gesture preview and live gesture recognition were not tested against a physical camera.
- A fresh dependency installation was not run. The active environment passed `pip check`.
- YouTube and other network-backed behavior was unit-tested with controlled test results, not exercised against live services.

## SUGGESTIONS (not applied)

- Decide and document Ruff rules, then address the remaining whole-project lint output in small reviewed batches.
- Rebuild `.ttsenv` after repairing the Python 3.12 installation, preserving the existing directory until a working replacement is ready.
- Repeat the HUD and microphone checks on the normal logged-in Windows desktop with the headset connected.

## HOW TO RUN

- Normal UI and voice mode: `start_jarvis.bat` or `.venv\Scripts\python.exe main.py --ui`
- Text mode: `.venv\Scripts\python.exe main.py --text`
- Debug mode: `.venv\Scripts\python.exe main.py --debug`
- Text mode with debug details: `.venv\Scripts\python.exe main.py --text --debug`
- Tests: `.venv\Scripts\python.exe -m unittest discover -v tests`
- Microphone check: `.venv\Scripts\python.exe voice\mic_test.py`
