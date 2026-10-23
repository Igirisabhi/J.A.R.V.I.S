# Jarvis settings and command corrections

Frequently edited commands and gestures use SQLite:

- `jarvis.db` stores custom voice commands, named hand-sign mappings, and
  camera-trained custom gestures.
- The Voice Commands and Gesture Controls UI update the database immediately.
- Existing entries in the old JSON sections are migrated automatically the first
  time Jarvis opens the database.
- Keep `command_corrections.json` for phonetic/mispronunciation corrections and
  keep `gesture_settings.json` for static camera settings and default mappings.

Valid edits to `settings.json` reload while Jarvis is running. Most settings take
effect on the next audio or speech operation. If you change the local
speech model or device, Jarvis loads the new model the next time offline
transcription is needed. Invalid JSON is ignored until the file is valid again.

- `audio`: speech volume, microphone sensitivity, recording duration, and
  interruption timing. Jarvis always uses the current Windows default input
  device; select a headset microphone in Windows Sound settings if desired.
- `voice`: wake phrase confidence and confirmation timing.
- `speech.online`: transcription providers, models, timeouts, and connections.
- `speech.local`: offline transcription model, device, compute mode, and speed.
- `speech.tts`: providers, voices, rate, timeout, and local fallback.
- `ui_settings.json`: HUD accent, animation and rotation, orbit speed and size,
  particle count, and telemetry refresh rate. UI settings reload when the file
  is saved while Jarvis is running.

To reduce background memory use, offline speech model loading is disabled at
startup and the multilingual `tiny` model is used only when offline transcription
is needed. The HUD also starts with motion and particles disabled and updates
system telemetry every 2.5 seconds. Edit `ui_settings.json` to change those
visual defaults; set `motion_enabled` and `rotation_enabled` to `true` or raise
`particle_count` to restore effects.

## Add a mispronunciation

Edit `command_corrections.json`. Under `corrections`, add what Jarvis heard as
the key and the command it should understand as the value:

```json
{
  "corrections": {
    "know pad": "Notepad"
  }
}
```

The correction file is checked on each utterance, so saved aliases take effect
immediately without restarting. It contains existing pronunciation fixes too;
you can edit or add entries there.

Values are clamped to safe ranges. Unknown settings keys are ignored. Jarvis
captures from the Windows default input and resamples only when needed by the
speech models.
