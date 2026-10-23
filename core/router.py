import json
import re
import sys
from typing import Any

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from core.ai import ask_groq, ask_gemini, ask_ai_structured, parse_structured_ai_response
from core.session_memory import remember_assistant, remember_user
from tools.system_tools import handle_command


# ============================================================
# KEYWORDS
# ============================================================

SIMPLE_KEYWORDS = [
    "hello", "hi", "hey", "what is", "what's", "who is", "who was",
    "when is", "when was", "where is", "where was", "why is", "why does",
    "how is", "how does", "how many", "how much", "tell me about",
    "define", "meaning of", "translate", "convert", "calculate",
]


COMPLEX_KEYWORDS = [
    "design", "architecture", "develop", "build", "create a project",
    "project", "explain", "analyze", "analysis", "compare", "debug",
    "code", "program", "implement", "research", "strategy", "plan",
    "cybersecurity", "machine learning", "deep learning", "write",
    "draft", "review", "refactor", "fix", "step by step", "pros and cons",
    "in detail", "guide", "tutorial", "specification", "requirements",
]


def _contains_keyword(text: str, keyword: str) -> bool:
    """Match full words/phrases so short tokens like 'hi' don't match inside words."""
    pattern = r"(?<!\w)" + r"\s+".join(map(re.escape, keyword.split())) + r"(?!\w)"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


# Capability / offer patterns to never flag
CAPABILITY_OFFER_REGEX = re.compile(
    r"\b(?:i can|i am able to|i'm able to|i could|i'm here to|i am here to|"
    r"help (?:you )?with|let me know|would you like|tasks like|such as|"
    r"feel free to|ready to|tell me what)\b",
    re.IGNORECASE,
)

# Completed action claim patterns (past-tense completion claims where no tool ran or succeeded)
COMPLETED_ACTION_RULES = [
    ("completed_open", re.compile(r"^(?:(?:i(?:'ve| have)?\s+)?(?:opened|launched|started)|(?:done|all set),\s*(?:i(?:'ve)?\s*)?(?:opened|launched|started))\b|\b(?:i(?:'ve| have)\s+(?:opened|launched|started))\b", re.IGNORECASE)),
    ("completed_action", re.compile(r"^(?:(?:i(?:'ve| have)?\s+)?(?:sent|created|deleted|closed|stopped|resumed)|(?:done|all set),\s*(?:i(?:'ve)?\s*)?(?:sent|created|deleted|closed))\b|\b(?:i(?:'ve| have)\s+(?:sent|created|deleted|closed|stopped|resumed))\b", re.IGNORECASE)),
    ("completed_play", re.compile(r"^(?:(?:i(?:'ve| have)?\s+)?(?:played|put on)|(?:i(?:'m| am)?\s+)?playing|now playing)\b|\b(?:i(?:'ve| have)\s+played|(?:done|all set),\s*(?:i(?:'ve)?\s*)?played|now playing\b|here is .+ playing\b)", re.IGNORECASE)),
]


def filter_hallucinated_action_claims(spoken: str, tool_succeeded: bool = False) -> str:
    """
    Filter hallucinated action claims:
    - Never flag descriptions of capabilities or offers ("I can help with...", "I'm here to help with tasks like...", "Let me know what you'd like to do").
    - Only flag when it claims a COMPLETED action that no tool actually ran ("I opened Chrome", "I've sent the message", "Done, I played it") AND no tool succeeded.
    - Strips only the offending sentence, or replaces with safe fallback: "Sorry, I couldn't do that."
    - Logs which rule matched.
    """
    if not spoken:
        return spoken
    if tool_succeeded:
        return spoken

    # Split into sentences
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", spoken) if s.strip()]
    if not sentences:
        sentences = [spoken.strip()]

    kept_sentences = []
    dropped = False

    for sentence in sentences:
        # Check capability / offer first: NEVER flag offers or capability descriptions
        if CAPABILITY_OFFER_REGEX.search(sentence):
            kept_sentences.append(sentence)
            continue

        matched_rule_name = None
        for rule_name, rule_regex in COMPLETED_ACTION_RULES:
            if rule_regex.search(sentence):
                matched_rule_name = rule_name
                break

        if matched_rule_name:
            print(f"⚠️ Hallucination filter matched rule '{matched_rule_name}' on: '{sentence}'")
            dropped = True
        else:
            kept_sentences.append(sentence)

    if not dropped:
        return spoken

    if kept_sentences:
        return " ".join(kept_sentences)

    return "Sorry, I couldn't do that."


# ============================================================
# STRUCTURED ACTION EXECUTION
# ============================================================

def execute_structured_action(action: str, args: dict[str, Any], prompt: str = "") -> tuple[bool, str]:
    """
    Execute a validated structured action returned by the AI router.
    Always returns (success: bool, message: str), never None.
    """
    if not action:
        return False, "No executable action was specified."

    action_lower = action.lower().strip()
    check_text = f"{prompt} {args.get('query', '')} {args.get('title', '')}".lower()
    user_explicitly_wants_youtube = (
        any(k in check_text for k in ("youtube", "you tube", "yt"))
        or "bad apple" in check_text
    )

    try:
        if action_lower in {"play_youtube", "play", "play_media"}:
            query = args.get("query") or args.get("title") or args.get("search") or ""
            browser = args.get("browser")
            if not query:
                return False, "I couldn't identify what media to play."

            # Only go to YouTube if the user explicitly asked for YouTube (or bad apple benchmark)
            if not user_explicitly_wants_youtube and action_lower == "play_youtube":
                from tools.system_tools import handle_play_command
                local_res = handle_play_command(f"play {query}")
                if local_res and not any(err in local_res.lower() for err in ("couldn't", "could not", "failed", "can't")):
                    return True, local_res
                return False, f"Name a website, such as YouTube, to choose where to play {query}."

            from tools.system_tools import play_youtube
            msg = play_youtube(query, browser=browser)
            if not msg:
                return False, f"I couldn't open YouTube search for {query}."
            failed = any(err in msg.lower() for err in ("couldn't", "could not", "failed", "can't"))
            return not failed, msg

        if action_lower in {"open_app", "launch"}:
            app = args.get("app") or args.get("name") or ""
            if not app:
                return False, "I couldn't identify which application to open."
            from tools.window_tools import open_app
            res = open_app(app)
            if not res:
                return False, f"I couldn't open {app}."
            failed = any(err in res.lower() for err in ("couldn't", "could not", "failed", "not found", "can't"))
            return not failed, res

        if action_lower in {"open_url", "navigate"}:
            url = args.get("url") or args.get("website") or ""
            browser = args.get("browser")
            if not url:
                return False, "I couldn't identify which URL to open."
            from tools.system_tools import open_url
            if not open_url(url, browser=browser):
                return False, f"I couldn't open {url}."
            return True, f"Opening {url}."

        if action_lower == "search":
            query = args.get("query") or ""
            destination = (args.get("destination") or "google").lower()
            if not query:
                return False, "I need a search query."
            if destination == "youtube":
                if not user_explicitly_wants_youtube:
                    destination = "google"
                else:
                    from tools.system_tools import search_youtube
                    res = search_youtube(query)
                    failed = not res or any(err in res.lower() for err in ("couldn't", "failed"))
                    return not failed, res or f"I couldn't search YouTube for {query}."
            from tools.system_tools import search_website_destination
            res = search_website_destination(destination, query)
            failed = not res or any(err in res.lower() for err in ("couldn't", "failed"))
            return not failed, res or f"I couldn't search {destination} for {query}."

        if action_lower == "command":
            cmd = args.get("command") or args.get("text") or ""
            if not cmd:
                return False, "I need a command to execute."
            from tools.system_tools import handle_command as sys_handle_command
            res = sys_handle_command(cmd)
            if not res:
                return False, f"I couldn't execute {cmd}."
            failed = any(err in res.lower() for err in ("couldn't", "could not", "failed", "can't"))
            return not failed, res

        return False, f"Unsupported action: {action}."
    except Exception as exc:
        print(f"⚠️ Action execution failed: {exc}")
        return False, f"I couldn't perform that action: {exc}."


def _parse_ai_result(raw_result: Any) -> dict[str, Any]:
    """Convert raw AI response (dict or string) into validated structured dict."""
    if isinstance(raw_result, dict):
        action = raw_result.get("action")
        arguments = raw_result.get("arguments", {})
        spoken = str(raw_result.get("spoken", "")).strip()
        return {
            "action": action,
            "arguments": arguments if isinstance(arguments, dict) else {},
            "spoken": spoken,
        }

    if isinstance(raw_result, str):
        try:
            return parse_structured_ai_response(raw_result)
        except Exception:
            # If plain text returned (e.g. from a test mock), treat as answer with no action
            return {
                "action": None,
                "arguments": {},
                "spoken": raw_result.strip(),
            }

    return {
        "action": None,
        "arguments": {},
        "spoken": "I couldn't process that response.",
    }


# ============================================================
# ROUTER
# ============================================================

def route(prompt: str, local_checked: bool = False, user_recorded: bool = False, abort_event=None) -> str:
    if abort_event and abort_event.is_set():
        return ""

    text = prompt.lower().strip()

    if not user_recorded:
        remember_user(prompt)

    # Keep direct callers on the same local-first route as the voice loop.
    if not local_checked:
        result = handle_command(prompt)
        if result is not None:
            if not (abort_event and abort_event.is_set()):
                remember_assistant(result)
            return result

    def _finish(ans: str) -> str:
        if not (abort_event and abort_event.is_set()):
            remember_assistant(ans)
        return ans

    prefer = "gemini" if any(_contains_keyword(text, keyword) for keyword in COMPLEX_KEYWORDS) else "groq"
    provider_name = "Gemini" if prefer == "gemini" else "Groq"
    print(f"🧠 Router → {provider_name} (structured)")

    raw_ai_result = None
    try:
        # Prefer structured call first
        raw_ai_result = ask_ai_structured(prompt, prefer=prefer)
    except Exception as structured_err:
        if abort_event and abort_event.is_set():
            return ""
        s_err = str(structured_err).splitlines()[0] if str(structured_err) else type(structured_err).__name__
        print(f"⚠️ Structured AI call warning: {s_err}. Attempting fallback...")
        try:
            # Fallback to direct ask_groq or ask_gemini
            raw_text = ask_groq(prompt) if prefer == "groq" else ask_gemini(prompt)
            raw_ai_result = _parse_ai_result(raw_text)
        except Exception as fallback_err:
            if abort_event and abort_event.is_set():
                return ""
            f_err = str(fallback_err).splitlines()[0] if str(fallback_err) else type(fallback_err).__name__
            print(f"⚠️ Fallback AI warning: {f_err}. Falling back to local rule-based routing...")
            local_fallback = handle_command(prompt)
            if local_fallback is not None:
                return _finish(local_fallback)
            return _finish("I'm unable to connect to my AI services right now.")

    parsed = _parse_ai_result(raw_ai_result)
    action = parsed.get("action")
    arguments = parsed.get("arguments", {})
    spoken = parsed.get("spoken", "")

    # If an action was identified, execute it!
    if action:
        success, action_message = execute_structured_action(action, arguments, prompt=prompt)
        if success:
            return _finish(action_message)
        else:
            # Action failed - state failure plainly and never claim success
            return _finish(action_message or "I couldn't open that.")

    # If action is None, filter hallucinated action claims
    if spoken:
        filtered = filter_hallucinated_action_claims(spoken, tool_succeeded=False)
        return _finish(filtered)

    if not spoken:
        return _finish("I couldn't process that request.")

    return _finish(spoken)


# ============================================================
# DIRECT TEST
# ============================================================

if __name__ == "__main__":
    question = input("Jarvis: ")
    response = route(question)
    print("\n🤖 Jarvis:")
    print(response)
