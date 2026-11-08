"""Window-only wording for the Quiet Desk interface.

`wording.py` stays the shared source for what the exported file also says (states,
gaps, coverage); this module holds the bar, rows, banners, dialogs and settings text
that only the window shows. Mic and System are audio sources, never people.
"""

from __future__ import annotations

WINDOW_TITLE = "Tadween Desk"
NAV = {  # key -> (full label, short label for the compact rail)
    "new": ("New meeting", "New"),
    "live": ("Current meeting", "Current"),
    "history": ("History", "History"),
    "settings": ("Settings and help", "Settings"),
}
NAV_FOOTER = "Transcribed on this PC.\nAudio is never saved."

# ---------------------------------------------------------------- meeting bar
PILL = {"rec": "Transcribing", "paused": "Paused", "blocked": "Paused — saving failed", "finalizing": "Finalizing",
        "saving": "Still saving"}
TITLE_STATE = {"rec": "Transcribing", "paused": "Paused", "blocked": "Paused", "finalizing": "Finalizing",
               "saving": "Saving"}
MIC_CHIP = {True: ("Mic included", "Included"), False: ("Mic excluded", "Excluded")}
MIC_NONE = "No mic"
BTN_MUTE = ("Mute mic", "Mute")
BTN_UNMUTE = ("Unmute mic", "Unmute")
BTN_PAUSE = ("Pause all", "Pause")
BTN_RESUME = "Resume"
BTN_STOP = ("Stop and save", "Stop")
BTN_LIVE = ("Live transcript", "Live")
BTN_LIVE_ACCESSIBLE = "Return to live transcript"

# ---------------------------------------------------------------- new meeting
NEW_H1 = "New meeting"
NEW_SUB = ("Transcribes your microphone and this computer's playback. Text is saved on this PC; audio is never "
           "saved.")
CARD_MEETING = "Meeting"
TITLE_HELP = "Optional. Left empty, the meeting is named by its start time. You can rename it later."
CARD_SOURCES = "Audio sources"
REFRESH = "Refresh devices"
REFRESH_DONE = "Device list refreshed — {mics} microphone(s), {outs} playback device(s)."
DEVICE_NONE = "None — don't transcribe"
START_MUTED = "Start with my mic excluded (muted in this app)"
START_MUTED_HELP = "You can include it any time with Unmute mic ({keys})."
SYSTEM_HELP = ("System audio is everything this PC plays on the selected output — the meeting app, videos, "
               "notification sounds. Close other audio you don't want transcribed.")
ONE_SOURCE = "If one source can't open, continue with the other"
ONE_SOURCE_HELP = "Otherwise you'll be asked before starting with one source."
ROUTING_LINK = "How audio routing works — mute, echo and headphones"
CARD_LANGUAGE = "Language and text"
LANG_LABEL = "Spoken language"
DRAFT = "Show draft text while someone speaks"
DRAFT_HELP = "Uses about twice the CPU. Draft text is replaced by the final text and is never saved."
READY_H = "Ready to start"
MODEL_TITLE = {"loading": "Checking the local speech model…", "ready": "Local speech model ready",
               "missing": "Speech model files are missing", "error": "The speech model could not be loaded"}
CHECK_AGAIN = "Check again"
READY_MIC = {"selected": "Microphone selected", "excluded": "Microphone selected — starts excluded",
             "none": "No microphone"}
READY_MIC_NONE_HELP = "Only System audio will be transcribed."
READY_SYSTEM = {"selected": "System audio selected", "none": "No System audio"}
READY_SYSTEM_NONE_HELP = "Only your microphone will be transcribed."
READY_OPTIONAL = "Optional"
START = "Start transcribing"
START_HINT = "Starts the selected sources. The controls stay at the top of every page."
START_WHY_LOADING = "Start is available when the speech model is ready."
START_WHY_MODEL = "Start is unavailable: the speech model is not ready."
START_WHY_DEVICES = "Select a microphone, a playback device, or both."
RECOVERED_TITLE = "{n} meeting(s) were still running when the app last closed"
RECOVERED_BODY = ("Their saved text is kept and they are marked “Ended incomplete”. Capture was not restarted.")
RECOVERED_OPEN = "Open it"
DISMISS = "Dismiss"
RETENTION_TITLE = "Choose how long History keeps transcripts"
RETENTION_BODY = "Nothing is deleted automatically until you choose. {days} days is suggested."
RETENTION_BTN = "Choose…"
SOURCE_FAILED_TITLE = {"system": "System audio could not open", "mic": "The microphone could not open"}
SOURCE_FAILED_BODY = "“{device}” is unavailable or in use exclusively by another app ({code})."
SOURCE_FAILED_CONSEQUENCE = {
    "system": "If you continue, only your microphone is transcribed — not the meeting audio from this PC.",
    "mic": "If you continue, only System audio is transcribed — not your voice."}
SOURCE_FAILED_CONTINUE = {"system": "Continue with mic only", "mic": "Continue with System audio only"}
SOURCE_FAILED_CHOOSE = "Choose another device"
CANCEL = "Cancel"

# --------------------------------------------------------------- live transcript
LIVE_SUB = "Started {time} · {language}{draft}"
LIVE_DRAFT_ON = " · Draft text on"
FOLLOW = ("Follow latest text", "Follow")
SIDE_TOGGLE = "Sources and status"
JUMP_LATEST = "Jump to latest"
ROW_DRAFT = "Draft — replaced when final"
ROW_UNSAVED = "NOT SAVED"
ROW_UNSAVED_HELP = "Waiting for saving to work again"
ROW_EDITED = "Edited"
ROW_REDACTED = "Text redacted"
ROW_REDACTED_CHIP = "Original and corrections removed"
NOT_A_LOSS = "Not a loss"
EVENT_LABEL = {
    "mic_excluded": "Mic excluded (muted in this app) — not transcribed",
    "paused": "Paused — nothing transcribed",
    "paused_by_user": "Paused — nothing transcribed",
    "stt_backlog": "No text — recognition fell behind",
    "device_switch": "No text — device switched",
    "stopped_undrained": "No text — not finished when Stop's time ran out",
    "text_not_saved": "Text could not be saved",
    "app_ended_during_meeting": "App ended during the meeting — later text may be missing",
    "save_failed": "Capture paused — saving failed",
    "segment_redacted": ROW_REDACTED,
}
DEVICE_LOST_LABEL = {"mic": "No mic text — microphone disconnected",
                     "system": "No System text — playback device disconnected"}
SIDE_SOURCES = "Sources"
SIDE_STATUS = "Status"
SOURCE_HEADING = {"mic": "Mic", "system": "System audio"}
SOURCE_CHIP = {"transcribing": ("Transcribing", "ok"), "excluded": ("Excluded", "warn"),
               "paused": ("Paused", "neutral"), "lost": ("Disconnected", "bad"),
               "reconnecting": ("Reconnecting", "info"), "failed": ("Could not open", "bad"),
               "none": ("Not selected", "neutral"), "stopped": ("Stopped", "neutral"),
               "starting": ("Starting", "info")}
METER_LEVEL = "Signal level"
METER_EXCLUDED = "Not transcribed while excluded — the mic is closed, so no level is read."
METER_PAUSED = "Not transcribed while paused"
METER_LOST = "No signal"
METER_STOPPED = "Capture stopped"
SYSTEM_NOTE = "Everything played on this output is transcribed."
RECONNECT = "Reconnect"
STATUS_KEEPING_UP = "Recognition keeping up"
STATUS_BEHIND = "Recognition {n} s behind — text will appear late"
STATUS_SAVED = "All final text saved to History"
STATUS_UNSAVED = "{n} line(s) NOT SAVED"
STATUS_SAVE_RETRY = "Saving problem ({code}) — retrying"
STATUS_GAPS = "{n} interval(s) without text"
STATUS_EXCLUDED = "Mic excluded {n} time(s) on purpose"
STATUS_PAUSES = "Paused {n} time(s)"
STATUS_HELP = "What do Mute mic and Pause all do?"
ALERT_LOST_TITLE = {"mic": "Microphone disconnected — your mic is not being transcribed",
                    "system": "Playback device disconnected — System audio is not being transcribed"}
ALERT_LOST_BODY = {"mic": "“{device}” stopped delivering audio. System audio continues. The missing time is marked "
                          "in the transcript.",
                   "system": "“{device}” stopped delivering audio. Your mic continues. The missing time is marked "
                             "in the transcript."}
ALERT_RECONNECT = {"mic": "Reconnect microphone", "system": "Reconnect playback device"}
ALERT_RECONNECTED_TITLE = {"mic": "Microphone reconnected", "system": "Playback device reconnected"}
ALERT_RECONNECTED_BODY = {True: "Mic transcription has resumed.",
                          False: "The mic stays excluded, as before. Use Unmute mic to include it."}
ALERT_RECONNECTED_SYSTEM = "System audio transcription has resumed."
ALERT_BACKLOG_TITLE = "Recognition is {n} s behind"
ALERT_BACKLOG_BODY = ("Text will appear late. Nothing has been lost so far; if the delay keeps growing, the affected "
                      "time is marked as having no text.")
ALERT_SAVE_FAILED_TITLE = "Saving failed — capture is paused so nothing more is lost"
ALERT_SAVE_FAILED_BODY = ("{n} line(s) are NOT SAVED yet and could be lost if the app closes. Free disk space, then "
                          "press Resume — or Stop and save.")
ALERT_SAVE_RECOVERED_TITLE = "Saving works again — the unsaved lines are now saved"
ALERT_SAVE_RECOVERED_BODY = "Press Resume to continue transcribing, or Stop and save."
ALERT_DEGRADED = {"mic": ("Transcribing your microphone only",
                          "System audio could not open, so meeting audio from this PC is not transcribed."),
                  "system": ("Transcribing System audio only",
                             "The microphone could not open, so your voice is not transcribed.")}
DETAILS = "Technical details"
FINAL_TITLE = "Finalizing — saving the last text"
FINAL_BODY = "Capture has stopped. Recognizing the last few seconds of speech (up to {s} s)."
STALLED_TITLE = "Still saving to History…"
STALLED_BODY = ("This is taking longer than usual. The app stays usable and you can read the transcript below. "
                "Save As opens as soon as saving finishes. Keep the app open so nothing is lost.")
STALLED_BROWSE = "Browse History meanwhile"
CLOSE_FINISHING = ("Saving is still finishing. Keep the window open until it finishes; Save As opens next, then the "
                   "window closes.")
EMPTY_LIVE = "Listening… Text appears a few seconds after someone speaks."

# ---------------------------------------------------------- stop, save, close
SAVED_COMPLETE = "Saved to History — complete. Everything captured was processed and saved."
SAVED_INCOMPLETE = "Saved to History — incomplete: some intervals have no text. See Coverage for details."
SAVEAS_CANCEL = "No file was saved. The transcript is saved in History — use Save As… any time."
REPLACE_TITLE = "Replace “{name}”?"
REPLACE_BODY = ("A file with this name already exists in that folder. Replacing it overwrites that file.\n\nYour "
                "transcript in History is not affected. Choose No to pick another name.")
WRITE_FAIL_TITLE = "The file was not saved"
WRITE_FAIL_BODY = ("“{name}” could not be saved ({code}). Your transcript is still saved in History, and an "
                   "existing file with this name was left unchanged. Choose another folder or name.")
CLOSE_ACTIVE_TITLE = "A meeting is still being transcribed"
CLOSE_ACTIVE_BODY = ("Stop and save to finish the transcript, then choose where to save a file before the window "
                     "closes. Text recognized so far is already in History (finishing takes up to {s} seconds).\n\n"
                     "Stop and save now? Choose Cancel to keep transcribing.")
CLOSE_CANCELLED = ("The window was not closed. The transcript is saved in History — press Save As… to save a file, "
                   "or close the window again.")

# ------------------------------------------------------------ saved transcript
CHIP_COMPLETE = "Complete"
CHIP_INCOMPLETE = "Incomplete · {n} gap(s)"
CHIP_INTERRUPTED = "Ended incomplete"
RENAME = "Rename"
TB_SAVE_AS = "Save As…"
TB_SAVE_AS_TIP = ("History saves recognized final text automatically on this PC; Save As… creates a separate "
                  "TXT/Markdown file where you choose. (Ctrl+S)")
TB = {"copy": "Copy", "correct": "Correct", "redact": "Redact", "history": "Edit history", "delete": "Delete meeting",
      "coverage": "Coverage"}
TB_SELECTION = "{n} line(s) selected"
TB_CLEAR = "Clear"
TB_HINT = "Click a line to select · Shift/Ctrl for more"
COV_H = "Coverage"
COV_TIMES = "Started {start}, stopped {stop}"
COV_TIMES_OPEN = "Started {start}; no stop time recorded"
COV_DRAIN = {True: "Final processing finished before saving", False: "Final processing did not finish",
             None: "Final processing was not recorded"}
COV_WHERE = "Saved in History on this PC"
COV_TIMELINE = "Event timeline"
COV_NONE = "No pauses, exclusions or gaps."
COV_EXPORT_NOTE = ("Files saved with Save As are separate copies. Later corrections, redactions or deletion do not "
                   "change them.")
TOAST_COPIED = "Copied {n} line(s) (original text, unchanged)."
CORRECT_TITLE = "Correct line at {time}"
CORRECT_LABEL = ("Original recognition:\n{original}\n\nCorrected text — saving creates a new version. The original "
                 "recognition stays in the line's edit history; files you saved before are not changed.")
TOAST_CORRECTED = "Correction saved — revision {n}."
HISTORY_TITLE = "Edit history — line at {time}"
HISTORY_ORIGINAL = "Original recognition"
HISTORY_CORRECTION = "Correction {n}"
HISTORY_CURRENT = "Current"
HISTORY_NOTE = "Search and Save As use the current version."
REDACT_TITLE = "Redact 1 line?"
REDACT_BODY = ("The text, its original recognition and every correction are removed from History and search. A "
               "content-free note of the interval stays with its time and source. This can't be undone.\n\nFiles "
               "you saved earlier with Save As are separate copies and are not changed.")
TOAST_REDACTED = "Text redacted. It is no longer shown or found in search."
DELETE_TITLE = "Delete “{title}”?"
DELETE_BODY = ("Removes the transcript, its corrections, search entries and coverage notes from this PC. This "
               "can't be undone.\n\nFiles saved with Save As are separate copies and are not deleted.")
TOAST_DELETED = "Meeting deleted from History."
RENAME_TITLE = "Rename meeting"
RENAME_LABEL = "Title — used in History, search and new Save As files:"
TOAST_RENAMED = "Meeting renamed."
MINUTES_TABS = ("Transcript", "Minutes", "Action items")
TABS_ACCESSIBLE = "Meeting views"

# --------------------------------------------------------------------- history
HIST_H1 = "History"
HIST_SUB = "{n} meeting(s) shown · saved automatically on this PC · {retention}"
HIST_RETENTION = {None: "kept until you delete them", "not_chosen": "retention not chosen yet"}
HIST_KEPT = "kept for {days} days"
HIST_SEARCH_PLACEHOLDER = "Search titles and transcript text — English or Arabic"
HIST_SEARCH_DESCRIPTION = "Searches titles and current transcript text, English or Arabic. Press Enter to search."
HIST_SEARCH = "Search"
HIST_CLEAR = "Clear search"
HIST_COLS = ("Title", "Date", "Length", "Status")
HIST_RESULTS = "{n} match(es) in {m} meeting(s) · current text only (redacted text is never found)"
HIST_TITLE_MATCH = "Title match"
HIST_OPEN_AT = "Open at {time}"
HIST_OPEN_MEETING = "Open meeting"
HIST_EMPTY = ("No meetings yet", "Transcripts are saved here automatically while you record. Nothing leaves this PC.",
              "New meeting")
HIST_NONE = ("No matches for “{q}”", "Search looks for the exact words in current meeting titles and transcript "
             "text. Try fewer or different words.")
HIST_BUSY = ("History is busy finishing a save", "Results may take a moment. You can keep working; try again shortly.")
HIST_ERROR = ("Search couldn't run", "History itself is intact. Try again, or open a meeting from the list.")
HIST_TRY_AGAIN = "Try again"
HIST_OPEN = "Open"
HIST_DELETE = "Delete"
LENGTH_UNKNOWN = "—"
LENGTH_UNKNOWN_ACCESSIBLE = "Length not recorded"
COPY_PATH = "Copy path"
COPY_HISTORY_PATH = "Copy History location"
TOAST_PATH = "History location copied."

# ------------------------------------------------------------------- settings
SET_H1 = "Settings and help"
READ_ONLY = "Read-only"
SET_APPEARANCE = "Appearance"
SET_THEME = "Theme"
SET_APPEARANCE_HELP = "Sizes follow Windows display scaling (100–200%)."
TOAST_THEME = "Theme: {name}."
SET_RETENTION = "Retention"
SET_RETENTION_HINT = "Cleanup runs at startup and every hour while the app is open."
SET_MODEL = "Local speech model"
SET_ROUTING = "Audio and routing help"
SET_STORAGE = "Storage"
SET_KEYS = "Keyboard shortcuts"
SET_KEYS_MUTE = "Mute / unmute mic"
SET_KEYS_HELP = ("Click the field, then press the new keys (Ctrl or Alt + a key). It works on every page, even "
                 "while typing.")
SET_KEYS_SAVE = "Save shortcut"
SET_KEYS_RESET = "Reset to {keys}"
SET_PRIVACY = "Privacy"
SET_ABOUT = "About and licences"
ABOUT_MISSING = ("The third-party notices are not part of this build. They are listed in NOTICES.md next to the "
                 "packaged app.")
MODEL_ROWS = (
    ("Recognition", "{model} (Q8_0, GGUF) through a local llama.cpp server on this PC"),
    ("Speech detection", "Silero VAD"),
    ("Files", "app\\models (checked with SHA-256 when loaded; never downloaded automatically)"),
    ("Network", "None outside this PC. The speech server only listens on this computer."),
)
STORAGE_ROWS = (
    ("Contents", "Transcript text, timestamps, source labels, corrections and coverage notes. No audio or video."),
    ("Save As copies", "Independent files wherever you chose. The app does not track or change them."),
)
PRIVACY_ROWS = (
    ("Audio", "Never saved. Short audio buffers exist only in memory while transcribing."),
    ("Network", "No telemetry, update checks or cloud transcription. Speech recognition runs on this PC."),
    ("Diagnostics", "Content-free: no transcript text or keys."),
    ("Sources", "Mic and System are audio sources, not identified people."),
)
SHORTCUT_EXTRA = (("Shift+↑/↓", "Extend line selection"), ("Esc", "Close dialog"))
