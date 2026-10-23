import json
import os
import re
import sys

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

from dotenv import load_dotenv
from google import genai
from groq import Groq
from core.session_memory import ai_messages, context_summary

load_dotenv()

gemini = None
groq = None


def _get_gemini_client():
    global gemini
    if gemini is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured.")
        # Keep command handling bounded when the network or Gemini endpoint is
        # unavailable. The speech clients already use an explicit timeout;
        # this client otherwise inherited an unbounded SDK default.
        from google.genai import types as genai_types
        gemini = genai.Client(
            api_key=api_key,
            http_options=genai_types.HttpOptions(timeout=15_000),
        )
    return gemini


def _get_groq_client():
    global groq
    if groq is None:
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY is not configured.")
        groq = Groq(api_key=api_key, timeout=15.0)
    return groq


GEMINI_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.6-flash",
]

GROQ_MODELS = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
]

GROQ_STRUCTURED_MAX_TOKENS = 600
GEMINI_STRUCTURED_MAX_TOKENS = 250

# ============================================================
# JARVIS VOICE SYSTEM PROMPTS
# ============================================================

JARVIS_SYSTEM = """
You are Jarvis, a desktop voice assistant.

Rules:
- Give VERY short answers.
- Answer in 1 to 3 sentences.
- Usually stay under 40 words.
- Use simple spoken English.
- Do not use Markdown.
- Do not use asterisks.
- Do not use bullet points.
- Do not use headings.
- Do not repeat the user's question.
- Do not give long explanations unless explicitly asked.
- If the user asks a simple question, answer simply.
- For commands, confirm briefly.
- Never say "Here is a detailed explanation".
- Never add unnecessary context.
"""

JARVIS_STRUCTURED_SYSTEM = """You are Jarvis, an advanced desktop voice assistant.
Respond ONLY with a single valid JSON object containing exactly these fields:
{
  "action": string or null,
  "arguments": object,
  "spoken": string
}

Field specifications:
1. "action":
   - "play_youtube": ONLY when the user explicitly asks to play, watch, or listen to something on YouTube (the user MUST explicitly mention "YouTube" or "YT").
   - "open_app": when the user wants to launch or open an application.
   - "open_url": when the user wants to navigate to a specific website.
   - "search": when the user explicitly asks to search the web or google something.
   - "command": when the user wants to execute a system command.
   - null: when the user asks a general question, chat, definition, explanation, informational query, or conversation (e.g., "what is...", "who is...", "wattage genius"). In these cases, answer directly in "spoken" and do not perform any browser action.
2. "arguments":
   - For "play_youtube": {"query": "<song or video title>"}
   - For "open_app": {"app": "<app name>"}
   - For "open_url": {"url": "<url or site>"}
   - For "search": {"query": "<search query>", "destination": "<google or website explicitly requested>"}
   - For "command": {"command": "<command text>"}
   - For null action: {}
3. "spoken":
   - What Jarvis speaks aloud to the user.
   - Must be a non-empty string in plain spoken English (1 to 3 sentences, strictly under 40 words, no markdown, no asterisks, no bullets).
   - If action is null, answer the user's question directly in "spoken".
   - CRITICAL SAFETY RULE: Never claim an action succeeded or that you opened/played something unless "action" is specified. If you cannot perform the action, set "action" to null and set "spoken" to state plainly that you cannot do it (e.g. "I can't open that").
"""

MAX_OUTPUT_TOKENS = 140


def clean_ai_text(text: str) -> str:
    """Clean AI output before displaying/sending to TTS."""
    if not text:
        return ""

    text = text.replace("**", "").replace("__", "").replace("*", "")

    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        line = line.lstrip("#").strip()
        if line.startswith("- "):
            line = line[2:].strip()
        if line.startswith("• "):
            line = line[2:].strip()
        line = re.sub(r"^\d+\.\s+", "", line)
        if line:
            lines.append(line)

    text = " ".join(lines)
    return " ".join(text.split()).strip()


_INCOMPLETE_RESPONSE_ENDINGS = {
    "a", "an", "the", "to", "and", "or", "but", "of", "for", "with",
    "from", "because", "if", "when", "while", "is", "are", "was", "were",
}


def _validated_ai_text(text: str, provider: str) -> str:
    """Reject empty or obviously cut-off answers before sending them to speech."""
    cleaned = clean_ai_text(text)
    if not cleaned:
        raise RuntimeError(f"{provider} returned an empty response.")

    words = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", cleaned)
    if words and words[-1].lower() in _INCOMPLETE_RESPONSE_ENDINGS:
        raise RuntimeError(f"{provider} returned an incomplete response.")
    return cleaned


def repair_json_string(s: str) -> str:
    """Attempt to repair truncated or lightly malformed JSON."""
    s = (s or "").strip()
    if not s:
        return ""

    start_idx = s.find("{")
    if start_idx == -1:
        return ""
    s = s[start_idx:]

    if "```" in s:
        s = s.split("```")[0].strip()

    in_string = False
    escape = False
    stack = []
    cleaned_chars = []

    for char in s:
        if escape:
            escape = False
            cleaned_chars.append(char)
            continue
        if char == "\\":
            escape = True
            cleaned_chars.append(char)
            continue
        if char == '"':
            in_string = not in_string
            cleaned_chars.append(char)
            continue
        if not in_string:
            if char in "{[":
                stack.append(char)
            elif char in "}]":
                if stack:
                    top = stack[-1]
                    if (char == "}" and top == "{") or (char == "]" and top == "["):
                        stack.pop()
        cleaned_chars.append(char)

    repaired = "".join(cleaned_chars)
    if in_string:
        repaired += '"'

    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)

    while stack:
        opener = stack.pop()
        if opener == "{":
            repaired += "}"
        elif opener == "[":
            repaired += "]"

    return repaired


_SPOKEN_EXTRACTION_KEYS = (
    "spoken", "response", "answer", "text", "reply", "content", "message",
    "output", "result", "speech", "say", "assistant", "assistant_reply",
    "assistant_response", "explanation", "description", "summary", "detail", "details"
)


def _extract_spoken_text(data: object) -> str:
    """Robustly extract spoken response from arbitrary dict/list structures."""
    if isinstance(data, str):
        return data.strip()

    if isinstance(data, dict):
        for k in _SPOKEN_EXTRACTION_KEYS:
            if k in data:
                extracted = _extract_spoken_text(data[k])
                if extracted:
                    return extracted

        # Check reasoning/thought fields if no direct text found
        for k in ("thought", "reasoning", "thoughts"):
            if k in data:
                extracted = _extract_spoken_text(data[k])
                if extracted:
                    return extracted

        # Check any remaining non-action string values
        for k, v in data.items():
            if k in {"action", "arguments", "destination", "query", "url", "app", "command", "status", "type"}:
                continue
            extracted = _extract_spoken_text(v)
            if extracted:
                return extracted

    elif isinstance(data, list):
        for item in data:
            extracted = _extract_spoken_text(item)
            if extracted:
                return extracted

    return ""


def tolerant_json_parse(text: str) -> dict:
    """
    Wrap JSON parsing in a tolerant parser:
    - strip code fences
    - extract the first {...} block
    - try to repair truncated JSON
    """
    if not text or not str(text).strip():
        raise ValueError("AI returned an empty response.")

    cleaned = str(text).strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = cleaned.strip()

    if "{" not in cleaned:
        raise ValueError("AI response contains no JSON object.")

    # Extract the first {...} block
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    candidate = match.group(0) if match else cleaned

    try:
        data = json.loads(candidate)
        if isinstance(data, dict):
            return data
    except Exception:
        pass

    repaired = repair_json_string(cleaned)
    if repaired:
        try:
            data = json.loads(repaired)
            if isinstance(data, dict):
                return data
        except Exception as exc:
            raise ValueError(f"AI returned invalid JSON: {exc}") from exc

    raise ValueError("AI response must be a JSON object.")


def parse_structured_ai_response(text: str) -> dict:
    """
    Validate and extract structured action from AI JSON response using tolerant parsing.
    Ensures action, arguments, and spoken are well-formed.
    If the AI returns a natural plain-text response without JSON, safely uses that text as spoken.
    """
    if not text or not str(text).strip():
        raise ValueError("AI returned an empty response.")

    cleaned_raw = str(text).strip()

    # If the response contains no JSON object, treat it directly as plain-text spoken response
    if "{" not in cleaned_raw:
        spoken_clean = clean_ai_text(cleaned_raw)
        if spoken_clean:
            return {
                "action": None,
                "arguments": {},
                "spoken": spoken_clean,
            }
        raise ValueError("AI returned an empty response.")

    data = tolerant_json_parse(cleaned_raw)

    action = data.get("action")
    if action is not None and not isinstance(action, str):
        action = None
    if action:
        action = action.strip()
        if action.lower() in {"none", "null", "false", ""}:
            action = None

    arguments = data.get("arguments", {})
    if arguments is None or not isinstance(arguments, dict):
        arguments = {}

    spoken = _extract_spoken_text(data)

    if not spoken:
        if action:
            if action == "play_youtube":
                query = arguments.get("query") or "video"
                spoken = f"Playing {query} on YouTube."
            elif action == "open_app":
                app = arguments.get("app") or "app"
                spoken = f"Opening {app}."
            elif action == "open_url":
                url = arguments.get("url") or "website"
                spoken = f"Opening {url}."
            elif action == "search":
                query = arguments.get("query") or "that"
                spoken = f"Searching for {query}."
            elif action == "command":
                cmd = arguments.get("command") or "command"
                spoken = f"Executing {cmd}."
            elif "open" in action:
                spoken = f"Opening {action}."
            else:
                spoken = f"Executing {action}."
        else:
            # Check if there is plain text outside the JSON braces
            outside_text = re.sub(r"\{.*\}", "", cleaned_raw, flags=re.DOTALL).strip()
            if outside_text:
                spoken = clean_ai_text(outside_text)

        if not spoken:
            raise ValueError("Field 'spoken' must be a non-empty string.")

    spoken = clean_ai_text(spoken)

    return {
        "action": action,
        "arguments": arguments,
        "spoken": spoken,
    }


def build_prompt(prompt: str, structured: bool = False) -> str:
    """Gemini's text format, built from the same history used by Groq."""
    turns = "\n".join(
        f"{message['role'].title()}: {message['content']}"
        for message in ai_messages(prompt)
    )
    sys_prompt = JARVIS_STRUCTURED_SYSTEM if structured else JARVIS_SYSTEM
    return f"{sys_prompt}\n\nSession context:\n{context_summary()}\n\nConversation:\n{turns}\nAssistant:"


# ============================================================
# STRUCTURED AI CALLS
# ============================================================

def _call_groq_chat(client, params: dict):
    """
    Call Groq chat completions with robust multi-stage fallback:
    1. Try with params (response_format={'type': 'json_object'} and reasoning_format='hidden')
    2. If reasoning_format is unsupported, try without reasoning_format
    3. If Groq's constrained JSON validator throws 400 json_validate_failed,
       retry immediately WITHOUT response_format so generation succeeds and our tolerant parser
       extracts the JSON or plain text cleanly.
    """
    try:
        return client.chat.completions.create(
            **params,
            reasoning_format="hidden",
        )
    except (TypeError, Exception) as e1:
        err1 = str(e1).lower()
        if "json_validate_failed" in err1 or "failed to validate json" in err1 or "400" in err1:
            params_no_format = dict(params)
            params_no_format.pop("response_format", None)
            try:
                return client.chat.completions.create(**params_no_format, reasoning_format="hidden")
            except (TypeError, Exception):
                return client.chat.completions.create(**params_no_format)

        try:
            return client.chat.completions.create(**params)
        except Exception as e2:
            err2 = str(e2).lower()
            if "json_validate_failed" in err2 or "failed to validate json" in err2 or "400" in err2:
                params_no_format = dict(params)
                params_no_format.pop("response_format", None)
                return client.chat.completions.create(**params_no_format)
            raise


def ask_groq_structured(prompt: str) -> dict:
    """
    Query Groq expecting a structured JSON action response.
    Falls through GROQ_MODELS with short retry, sufficient token limits for reasoning models,
    and automatic unconstrained fallback if Groq's JSON validator fails with 400.
    """
    import time
    last_error = None
    messages = [
        {
            "role": "system",
            "content": f"{JARVIS_STRUCTURED_SYSTEM}\n\nSession context:\n{context_summary()}",
        },
        *ai_messages(prompt),
    ]

    for model in GROQ_MODELS:
        for attempt in range(2):
            try:
                params = {
                    "model": model,
                    "messages": messages,
                    "temperature": 0.1,
                    "max_tokens": GROQ_STRUCTURED_MAX_TOKENS,
                    "response_format": {"type": "json_object"},
                }
                response = _call_groq_chat(_get_groq_client(), params)

                text = response.choices[0].message.content
                if not text or not text.strip():
                    raise RuntimeError(f"Groq model {model} returned an empty response.")

                return parse_structured_ai_response(text)
            except Exception as e:
                last_error = e
                err_msg = str(e).splitlines()[0] if str(e) else type(e).__name__
                if "rate_limit" in str(e).lower() or "429" in str(e):
                    break
                if attempt == 0:
                    time.sleep(0.3)

    raise RuntimeError(f"All Groq models failed: {str(last_error).splitlines()[0] if last_error else 'Unknown'}")


def ask_gemini_structured(prompt: str) -> dict:
    """
    Query Gemini expecting a structured JSON action response.
    Retries once with short backoff, falls through model list (flash -> flash-lite),
    and logs a one-line warning (no stack trace) on failure.
    """
    import time
    last_error = None
    final_prompt = build_prompt(prompt, structured=True)

    for model in GEMINI_MODELS:
        for attempt in range(2):
            try:
                print(f"🧠 Gemini → {model} (attempt {attempt + 1})")
                response = _get_gemini_client().models.generate_content(
                    model=model,
                    contents=final_prompt,
                    config={
                        "max_output_tokens": GEMINI_STRUCTURED_MAX_TOKENS,
                        "temperature": 0.1,
                        "response_mime_type": "application/json",
                    },
                )
                raw_text = getattr(response, "text", "") or ""
                return parse_structured_ai_response(raw_text)
            except Exception as e:
                last_error = e
                err_msg = str(e).splitlines()[0] if str(e) else type(e).__name__
                print(f"⚠️ Gemini warning ({model}, attempt {attempt + 1}): {err_msg}")
                if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                    break
                if attempt == 0:
                    time.sleep(0.5)

    raise RuntimeError(f"All Gemini models failed: {str(last_error).splitlines()[0] if last_error else 'Unknown'}")


def ask_ai_structured(prompt: str, prefer: str = "groq") -> dict:
    """Query AI with fallback between Groq and Gemini, returning validated dict."""
    if prefer == "gemini":
        try:
            return ask_gemini_structured(prompt)
        except Exception as e1:
            err_line = str(e1).splitlines()[0] if str(e1) else type(e1).__name__
            print(f"⚠️ Gemini failed: {err_line}. Falling back to Groq...")
            return ask_groq_structured(prompt)
    else:
        try:
            return ask_groq_structured(prompt)
        except Exception as e1:
            err_line = str(e1).splitlines()[0] if str(e1) else type(e1).__name__
            print(f"⚠️ Groq failed: {err_line}. Falling back to Gemini...")
            return ask_gemini_structured(prompt)


# ============================================================
# UNSTRUCTURED BACKWARDS-COMPATIBLE CALLS
# ============================================================

def ask_gemini(prompt: str) -> str:
    try:
        data = ask_gemini_structured(prompt)
        return data["spoken"]
    except Exception:
        # Fallback to direct generation if structured fails
        last_error = None
        final_prompt = build_prompt(prompt, structured=False)
        for model in GEMINI_MODELS:
            try:
                response = _get_gemini_client().models.generate_content(
                    model=model,
                    contents=final_prompt,
                    config={
                        "max_output_tokens": MAX_OUTPUT_TOKENS,
                        "temperature": 0.2,
                    },
                )
                return _validated_ai_text(response.text, "Gemini")
            except Exception as e:
                last_error = e
                continue
        raise RuntimeError(f"All Gemini models failed: {last_error}")


def ask_groq(prompt: str) -> str:
    try:
        data = ask_groq_structured(prompt)
        return data["spoken"]
    except Exception:
        last_error = None
        messages = [
            {
                "role": "system",
                "content": f"{JARVIS_SYSTEM}\n\nSession context:\n{context_summary()}",
            },
            *ai_messages(prompt),
        ]
        for model in GROQ_MODELS:
            try:
                params = {
                    "model": model,
                    "messages": messages,
                    "temperature": 0.2,
                    "max_tokens": GROQ_STRUCTURED_MAX_TOKENS,
                }
                response = _call_groq_chat(_get_groq_client(), params)
                text = response.choices[0].message.content
                if not text or not text.strip():
                    continue
                return _validated_ai_text(text, "Groq")
            except Exception as e:
                last_error = e
                continue
        raise RuntimeError(f"All Groq models failed: {last_error}")


# ============================================================
# DIRECT TEST
# ============================================================

if __name__ == "__main__":
    question = input("Test AI: ")
    print("\n⚡ Testing Structured AI...")
    try:
        structured = ask_ai_structured(question)
        print("\n🤖 Jarvis Structured:")
        print(structured)
    except Exception as exc:
        print(f"\n❌ Error: {exc}")
