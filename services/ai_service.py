import os
import re
import time
import traceback
import json
from urllib import request, error
from models.system_config import SystemConfig
from services.failure_dict import lookup_failure
from services.historical_search import search_similar_failures

# Models to try in order (fallback if quota exhausted on one)
GEMINI_MODELS = ["gemini-2.0-flash-lite", "gemini-2.0-flash", "gemini-2.5-flash"]
CIRCUIT_DEFAULT_MODELS = ["gemini-3.1-flash-lite", "gpt-5-nano"]

# Retry config (like stock_analysis)
MAX_RETRIES = 2
RETRY_DELAY = 30  # seconds


def _get_api_keys():
    """Get all Gemini API keys: database config first, then env var fallback.
    Supports comma-separated keys for quota rotation."""
    keys = []
    db_key = SystemConfig.get_value("gemini_api_key")
    if db_key:
        keys.extend([k.strip() for k in db_key.split(",") if k.strip()])
    env_key = os.environ.get("GEMINI_API_KEY", "")
    if env_key:
        for k in env_key.split(","):
            k = k.strip()
            if k and k not in keys:
                keys.append(k)
    return keys


def _get_circuit_config():
    """Read CIRCUIT API config from DB/env fallback."""
    endpoint = (SystemConfig.get_value("circuit_api_endpoint") or "").strip()
    app_key = (SystemConfig.get_value("circuit_app_key") or "").strip()
    access_token = (SystemConfig.get_value("circuit_access_token") or "").strip()
    model = (SystemConfig.get_value("circuit_model") or "").strip()
    if not model:
        model = CIRCUIT_DEFAULT_MODELS[0]
    return {
        "endpoint": endpoint,
        "app_key": app_key,
        "access_token": access_token,
        "model": model,
        "enabled": bool(endpoint and app_key and access_token),
    }


def _call_circuit_api(circuit_cfg, prompt, model=None):
    """Call CIRCUIT API using correct Cisco CIRCUIT format (api-key auth + user field with appkey)."""
    endpoint = circuit_cfg["endpoint"]
    app_key = circuit_cfg["app_key"]
    access_token = circuit_cfg["access_token"]
    use_model = (model or circuit_cfg.get("model") or "").strip() or CIRCUIT_DEFAULT_MODELS[0]

    # Support template endpoints like .../deployments/{MODEL_NAME}/chat/completions?api-version=...
    endpoint = endpoint.replace("{MODEL_NAME}", use_model).replace("{model_name}", use_model)

    # CIRCUIT-specific payload: user field contains appkey as JSON string
    payload = {
        "user": json.dumps({"appkey": app_key}),
        "messages": [{"role": "user", "content": prompt}],
    }

    req = request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "api-key": access_token,  # CIRCUIT uses api-key header, not Authorization: Bearer
        },
    )

    try:
        with request.urlopen(req, timeout=45) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body)
    except error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {err_body or e.reason}")
    except error.URLError as e:
        raise RuntimeError(f"Network error: {e.reason}")

    # Parse CIRCUIT response (OpenAI format: choices is a list)
    if isinstance(data, dict):
        choices = data.get("choices")
        if isinstance(choices, list) and len(choices) > 0:
            msg = choices[0].get("message", {})
            content = msg.get("content", "")
            if content:
                return content

    raise RuntimeError(f"Unexpected CIRCUIT response: {str(data)[:300]}")


def analyze_log_with_ai(
    log_content,
    failure="",
    defect_class="",
    station="",
    bu="",
    keywords="",
    exclude_id=None,
    force_circuit=False,
):
    """Analyze log content using CIRCUIT/Gemini with fallback.

    Returns: {'success': bool, 'source': str, 'root_cause': str, 'action': str, 'details': list|None}
    """
    # Tier 0: Try CIRCUIT API first (if configured)
    circuit_cfg = _get_circuit_config()
    ai_error = None
    circuit_error = None
    circuit_token_expired = False

    def _is_token_error(err_text):
        err_lower = (err_text or "").lower()
        return any(
            k in err_lower
            for k in (
                "http 401",
                "jwt",
                "oauth",
                "access token",
                "token expired",
                "failedtoresolvevariable",
                "validate token",
            )
        )

    if force_circuit:
        if not circuit_cfg["enabled"]:
            return {
                "success": False,
                "source": "circuit",
                "model": circuit_cfg.get("model") or CIRCUIT_DEFAULT_MODELS[0],
                "root_cause": None,
                "action": None,
                "suggestion": None,
                "details": None,
                "ai_error": "CIRCUIT is not configured. Please enter Access Token in Settings.",
                "circuit_token_expired": False,
                "circuit_error": None,
            }

        try:
            prompt = _build_prompt(bu, station, failure, defect_class, log_content, keywords)
            result = _call_circuit_api(circuit_cfg, prompt)
            if result:
                root_cause, action = _parse_ai_response(result)
                return {
                    "success": True,
                    "source": "circuit",
                    "model": circuit_cfg.get("model") or CIRCUIT_DEFAULT_MODELS[0],
                    "root_cause": root_cause,
                    "action": action,
                    "suggestion": result,
                    "details": None,
                    "circuit_token_expired": False,
                    "circuit_error": None,
                }
        except Exception as e:
            circuit_error = str(e)
            circuit_token_expired = _is_token_error(circuit_error)
            return {
                "success": False,
                "source": "circuit",
                "model": circuit_cfg.get("model") or CIRCUIT_DEFAULT_MODELS[0],
                "root_cause": None,
                "action": None,
                "suggestion": None,
                "details": None,
                "ai_error": f"CIRCUIT failed: {circuit_error}",
                "circuit_token_expired": circuit_token_expired,
                "circuit_error": circuit_error,
            }

    if circuit_cfg["enabled"]:
        try:
            prompt = _build_prompt(bu, station, failure, defect_class, log_content, keywords)
            result = _call_circuit_api(circuit_cfg, prompt)
            if result:
                root_cause, action = _parse_ai_response(result)
                return {
                    "success": True,
                    "source": "circuit",
                    "model": circuit_cfg.get("model") or CIRCUIT_DEFAULT_MODELS[0],
                    "root_cause": root_cause,
                    "action": action,
                    "suggestion": result,
                    "details": None,
                    "circuit_token_expired": False,
                }
        except Exception as e:
            circuit_error = str(e)
            ai_error = f"CIRCUIT unavailable: {e}"
            circuit_token_expired = _is_token_error(circuit_error)

    # Tier 1: Try Google Gemini AI (with multiple key rotation)
    api_keys = _get_api_keys()
    for api_key in api_keys:
        try:
            result, used_model = _call_gemini(
                api_key,
                log_content,
                failure,
                defect_class,
                station,
                bu,
                keywords,
                return_model=True,
            )
            if result:
                root_cause, action = _parse_ai_response(result)
                return {
                    "success": True,
                    "source": "ai",
                    "model": used_model,
                    "root_cause": root_cause,
                    "action": action,
                    "suggestion": result,
                    "details": None,
                    "circuit_token_expired": circuit_token_expired,
                    "circuit_error": circuit_error,
                }
        except Exception as e:
            ai_error = str(e) if not ai_error else ai_error
            err_str = ai_error.lower()
            if any(
                k in err_str
                for k in ("quota", "resource_exhausted", "429", "503", "unavailable", "overloaded", "high demand")
            ):
                print(f"API key ...{api_key[-6:]} unavailable, trying next key...")
                continue
            print(f"Gemini API error: {e}")
            traceback.print_exc()
            break

    # Tier 2: Search historical data
    if failure:
        similar = search_similar_failures(failure, station=station, bu=bu, exclude_id=exclude_id)
        if similar:
            return {
                "success": True,
                "source": "history",
                "root_cause": similar[0].get("root_cause", ""),
                "action": similar[0].get("action", ""),
                "suggestion": similar[0].get("root_cause", ""),
                "details": similar,
                "ai_error": ai_error,
                "circuit_token_expired": circuit_token_expired,
                "circuit_error": circuit_error,
            }

    # Tier 3: Static failure dictionary
    if failure and bu:
        dict_result = lookup_failure(bu, failure)
        if dict_result:
            defect_cls, defect_val, root_cause = dict_result
            return {
                "success": True,
                "source": "dict",
                "root_cause": root_cause,
                "action": "",
                "suggestion": root_cause,
                "details": [{"defect_class": defect_cls, "defect_value": defect_val, "root_cause": root_cause}],
                "ai_error": ai_error,
                "circuit_token_expired": circuit_token_expired,
                "circuit_error": circuit_error,
            }

    # Tier 4: No suggestion available
    return {
        "success": False,
        "source": "none",
        "root_cause": None,
        "action": None,
        "suggestion": None,
        "details": None,
        "ai_error": ai_error
        or ("No API key configured. Set GEMINI_API_KEY in .env or Settings page." if not api_keys else None),
        "circuit_token_expired": circuit_token_expired,
        "circuit_error": circuit_error,
    }


def _parse_ai_response(text):
    """Parse AI response to extract Root Cause and Action separately."""
    root_cause = ""
    action = ""

    # Try to parse structured response in multiple languages.
    # This handles CIRCUIT outputs like:
    # - Root Cause / Recommended Action
    # - 根本原因 / 操作
    # - Nguyen nhan goc / Hanh dong
    rc_labels = [
        r"Root\s*Cause",
        r"Recommended\s*Root\s*Cause",
        r"\u6839\u672c\u539f\u56e0",
        r"\u539f\u56e0",
        r"Nguyen\s*nhan\s*goc",
    ]
    action_labels = [
        r"(?:Recommended\s+)?Action",
        r"\u64cd\u4f5c",           # 操作
        r"\u5efa\u8bae\u64cd\u4f5c",  # 建议操作
        r"\u5904\u7406\u65b9\u6848",  # 处理方案
        r"\u63aa\u65bd",           # 措施
        r"\u5efa\u8bae\u63aa\u65bd",  # 建议措施
        r"\u884c\u52a8",           # 行动
        r"\u89e3\u51b3\u65b9\u6848",  # 解决方案
        r"\u5efa\u8bae",           # 建议
        r"Hanh\s*dong",
        r"H[àa]nh\s*[Đđ][oô]ng",  # Hành Động (Vietnamese with diacritics)
        r"Bi[eệ]n\s*ph[aá]p",     # Biện pháp (Vietnamese)
    ]
    rc_pattern = "(?:" + "|".join(rc_labels) + ")"
    action_pattern = "(?:" + "|".join(action_labels) + ")"

    rc_match = re.search(
        rf"{rc_pattern}[:\uff1a\s]*(.+?)(?={action_pattern}[:\uff1a\s]|$)", text, re.IGNORECASE | re.DOTALL
    )
    action_match = re.search(rf"{action_pattern}[:\uff1a\s]*(.+?)$", text, re.IGNORECASE | re.DOTALL)

    if rc_match:
        root_cause = rc_match.group(1).strip()
    if action_match:
        raw_action = action_match.group(1).strip()
        # Ensure numbered lines are on separate lines
        raw_action = re.sub(r"(?<!\n)(\d+\.\s)", r"\n\1", raw_action)
        action = raw_action.strip()

    # Fallback: if parsing failed, put everything in root_cause
    if not root_cause and not action:
        root_cause = text.strip()

    # Strip all markdown formatting: **bold**, *italic*, `code`
    root_cause = _strip_markdown(root_cause)
    action = _strip_markdown(action)

    return root_cause, action


def _strip_markdown(text):
    """Remove markdown formatting from text."""
    if not text:
        return text
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)  # **bold**
    text = re.sub(r"\*(.+?)\*", r"\1", text)  # *italic*
    text = re.sub(r"`(.+?)`", r"\1", text)  # `code`
    return text.strip()


def beautify_root_cause_action(root_cause, action):
    """Use Gemini AI to beautify/improve Root Cause and Action text.

    Returns: {'success': bool, 'root_cause': str, 'action': str, 'error': str|None}
    """
    api_keys = _get_api_keys()
    circuit_cfg = _get_circuit_config()
    if not api_keys and not circuit_cfg["enabled"]:
        return {"success": False, "error": "No AI config found. Set CIRCUIT API or GEMINI API key in Settings."}

    prompt = f"""You are a technical writing expert for manufacturing defect reports.
Your task is to improve the clarity and readability of the following Root Cause and Action text.

Rules:
- Keep the original technical meaning and facts UNCHANGED.
- Improve grammar, sentence structure, and clarity.
- Use concise professional language suitable for engineering reports.
- Plain text ONLY. No markdown, no bold (**), no asterisks, no bullet symbols.
- Root Cause: 1-3 clear, concise sentences.
- Action: numbered steps. Keep the same number of steps, just improve wording.
- If the original text is already good, return it with only minor improvements.
- Do NOT add new information or diagnosis that isn't in the original text.

Original Root Cause:
{root_cause}

Original Action:
{action}

Format your response EXACTLY as:
Root Cause: [improved text]
Action:
1. [improved step]
2. [improved step]
..."""

    if circuit_cfg["enabled"]:
        try:
            result_text = _call_circuit_api(circuit_cfg, prompt)
            if result_text:
                new_rc, new_action = _parse_ai_response(result_text)
                return {
                    "success": True,
                    "root_cause": new_rc or root_cause,
                    "action": new_action or action,
                }
        except Exception:
            pass

    for api_key in api_keys:
        try:
            try:
                from google import genai

                client = genai.Client(api_key=api_key)
                for model_name in GEMINI_MODELS:
                    try:
                        response = client.models.generate_content(model=model_name, contents=prompt)
                        result_text = response.text
                        new_rc, new_action = _parse_ai_response(result_text)
                        return {
                            "success": True,
                            "root_cause": new_rc or root_cause,
                            "action": new_action or action,
                        }
                    except Exception as e:
                        err_str = str(e).lower()
                        if "quota" in err_str or "429" in err_str:
                            continue
                        raise
            except ImportError:
                result_text = _call_gemini_legacy(api_key, prompt, "", "", "", "", "")
                if result_text:
                    new_rc, new_action = _parse_ai_response(result_text)
                    return {
                        "success": True,
                        "root_cause": new_rc or root_cause,
                        "action": new_action or action,
                    }
        except Exception as e:
            err_str = str(e).lower()
            if "quota" in err_str or "429" in err_str:
                continue
            return {"success": False, "error": str(e)}

    return {"success": False, "error": "All API keys exhausted (quota). Please try again later."}


def translate_root_cause_action(root_cause, action, target_lang):
    """Translate Root Cause and Action text using Gemini AI.

    Args:
        root_cause: Root Cause text to translate
        action: Action text to translate
        target_lang: Target language code ('zh' for Chinese, 'vi' for Vietnamese)

    Returns: {'success': bool, 'root_cause': str, 'action': str, 'error': str|None}
    """
    api_keys = _get_api_keys()
    circuit_cfg = _get_circuit_config()
    if not api_keys and not circuit_cfg["enabled"]:
        return {
            "success": False,
            "error": "No AI config found. Set CIRCUIT API or GEMINI API key in Settings.",
        }

    lang_names = {"zh": "Chinese (Simplified)", "vi": "Vietnamese", "en": "English"}
    lang_name = lang_names.get(target_lang, target_lang)

    combined = ""
    if root_cause:
        combined += f"Root Cause:\n{root_cause}\n\n"
    if action:
        combined += f"Action:\n{action}"

    prompt = (
        f"Translate the following manufacturing defect report text into {lang_name}.\n"
        "Keep technical terms accurate. Output ONLY the translated text, no explanations.\n"
        "Preserve the original formatting (numbered steps, line breaks).\n"
        "IMPORTANT: Keep the section labels 'Root Cause:' and 'Action:' in English exactly as-is. Only translate the content after each label.\n\n"
        f"{combined.strip()}"
    )

    if circuit_cfg["enabled"]:
        try:
            result = _call_circuit_api(circuit_cfg, prompt)
            if result:
                t_rc, t_action = _parse_ai_response(result)
                return {
                    "success": True,
                    "root_cause": t_rc or root_cause,
                    "action": t_action or action,
                }
        except Exception:
            pass

    for api_key in api_keys:
        try:
            try:
                from google import genai

                client = genai.Client(api_key=api_key)
                for model_name in GEMINI_MODELS:
                    try:
                        response = client.models.generate_content(model=model_name, contents=prompt)
                        result = response.text.strip()
                        t_rc, t_action = _parse_ai_response(result)
                        return {
                            "success": True,
                            "root_cause": t_rc or root_cause,
                            "action": t_action or action,
                        }
                    except Exception as e:
                        if "quota" in str(e).lower() or "429" in str(e):
                            continue
                        raise
            except ImportError:
                result = _call_gemini_legacy(api_key, prompt, "", "", "", "", "")
                if result:
                    t_rc, t_action = _parse_ai_response(result)
                    return {
                        "success": True,
                        "root_cause": t_rc or root_cause,
                        "action": t_action or action,
                    }
        except Exception as e:
            if "quota" in str(e).lower() or "429" in str(e):
                continue
            return {"success": False, "error": str(e)}

    return {"success": False, "error": "All API keys exhausted. Please try again later."}


def _build_prompt(bu, station, failure, defect_class, log_content, keywords=""):
    """Build the shared AI analysis prompt."""
    keywords_section = ""
    if keywords:
        keywords_section = f"\nUser-provided Keywords/Hints: {keywords}\nIMPORTANT: Pay special attention to the keywords above. They indicate the engineer's suspected direction for root cause analysis. Use them to guide your diagnosis.\n"

    retest_rule = ""
    if bu and bu.strip().upper() == "CRBU":
        retest_rule = "\n- The last action step must always be: Retest and confirm PASS."

    return f"""You are a manufacturing defect analysis expert for Cisco networking equipment.
Analyze the following test logs (sequence log and buffer log) and diagnose the root cause.

BU: {bu}
Station: {station}
Failure Step: {failure}
Defect Class: {defect_class}
{keywords_section}
{log_content[:8000]}

IMPORTANT formatting rules:
- Plain text ONLY. No markdown, no bold (**), no asterisks, no special formatting.
- Root Cause: ONE single sentence summarizing the root cause. Be concise and direct.
- Action: exactly 3 numbered steps.{retest_rule}
  Only include steps for the actual cause category:
  * If operator issue: only operator-related steps
  * If test program issue: only test program-related steps
  * If test station/equipment issue: only station/equipment-related steps

Format your response EXACTLY as:
Root Cause: [one sentence]
Action:
1. [step]
2. [step]
3. [step]"""


def _call_gemini(api_key, log_content, failure, defect_class, station, bu, keywords="", return_model=False):
    """Call Google Gemini API with retry-on-rate-limit (like stock_analysis)."""
    try:
        from google import genai
    except ImportError:
        text = _call_gemini_legacy(api_key, log_content, failure, defect_class, station, bu, keywords)
        return (text, "gemini-legacy") if return_model else text

    client = genai.Client(api_key=api_key)
    prompt = _build_prompt(bu, station, failure, defect_class, log_content, keywords)

    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        for model_name in GEMINI_MODELS:
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                )
                return (response.text, model_name) if return_model else response.text
            except Exception as e:
                last_error = e
                err_str = str(e).lower()
                # Quota exhausted (429) — no point retrying, skip to next model/key
                if any(k in err_str for k in ("quota", "resource_exhausted", "429")):
                    print(f"Model {model_name} quota exhausted, trying next model...")
                    continue
                # Transient server error (503) — worth retrying after delay
                if any(k in err_str for k in ("503", "unavailable", "overloaded", "high demand")):
                    print(f"Model {model_name} temporarily unavailable, trying next model...")
                    continue
                raise

        # Check if error was quota (don't retry) vs transient (retry with delay)
        last_err_str = str(last_error).lower()
        if any(k in last_err_str for k in ("quota", "resource_exhausted", "429")):
            # Quota won't reset in seconds, no point waiting
            break

        if attempt < MAX_RETRIES:
            delay = _parse_retry_delay(str(last_error)) or RETRY_DELAY
            print(f"All models unavailable. Waiting {delay}s before retry {attempt + 2}/{MAX_RETRIES + 1}...")
            time.sleep(delay)

    raise last_error


def _parse_retry_delay(error_str):
    """Extract retry delay from Gemini error message (e.g. 'Please retry in 57.2s')."""
    match = re.search(r"retry in (\d+(?:\.\d+)?)s", error_str, re.IGNORECASE)
    if match:
        return min(int(float(match.group(1))) + 2, 90)  # cap at 90s, add 2s buffer
    return None


def _call_gemini_legacy(api_key, log_content, failure, defect_class, station, bu, keywords=""):
    """Fallback: Call Gemini using deprecated google.generativeai SDK."""
    import google.generativeai as genai

    genai.configure(api_key=api_key)

    prompt = _build_prompt(bu, station, failure, defect_class, log_content, keywords)

    last_error = None
    for model_name in GEMINI_MODELS:
        try:
            model = genai.GenerativeModel(model_name)
            response = model.generate_content(prompt)
            return response.text
        except Exception as e:
            last_error = e
            err_str = str(e).lower()
            if "quota" in err_str or "resource_exhausted" in err_str or "429" in err_str:
                print(f"Quota exhausted for {model_name}, trying next model...")
                continue
            raise

    raise last_error


def test_ai_connection(api_key):
    """Test if the Gemini API key is valid."""
    try:
        from google import genai

        client = genai.Client(api_key=api_key)
        for model_name in GEMINI_MODELS:
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents="Say 'connected' if you can read this.",
                )
                return True, f"[{model_name}] {response.text}"
            except Exception as e:
                if "quota" in str(e).lower() or "429" in str(e):
                    continue
                raise
        return False, "All models quota exhausted. Check billing at https://ai.google.dev"
    except ImportError:
        try:
            import google.generativeai as genai

            genai.configure(api_key=api_key)
            model = genai.GenerativeModel("gemini-2.0-flash")
            response = model.generate_content("Say 'connected' if you can read this.")
            return True, response.text
        except Exception as e:
            return False, str(e)
    except Exception as e:
        return False, str(e)


def test_circuit_connection(endpoint, app_key, access_token, model=""):
    """Test CIRCUIT API connectivity using the configured endpoint and credentials."""
    cfg = {
        "endpoint": (endpoint or "").strip(),
        "app_key": (app_key or "").strip(),
        "access_token": (access_token or "").strip(),
        "model": (model or "").strip() or CIRCUIT_DEFAULT_MODELS[0],
    }
    if not cfg["endpoint"] or not cfg["app_key"] or not cfg["access_token"]:
        return False, "Endpoint/AppKey/Access Token are required"
    try:
        text = _call_circuit_api(cfg, "Say connected.")
        return True, f"[{cfg['model']}] {text[:120]}"
    except Exception as e:
        return False, str(e)
