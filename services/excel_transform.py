"""
Excel DF/DR Transform Service
==============================
Transforms source DF/DR Excel to target temp.xlsx format.
Uses CIRCUIT AI API to classify failure steps.

Config data (BU prefixes, defect classes, repair rules) is loaded from
config/transform_config.json so it can be shared across projects.
"""

import os
import re
import json
import time
import random
from datetime import timedelta
from urllib import request, error

import pandas as pd

# ── Load config from JSON ──────────────────────────────────────────────────────

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "transform_config.json")


def _load_config(path=None):
    """Load transform config from JSON file."""
    p = path or _CONFIG_PATH
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


_config = _load_config()

BU_PREFIX = _config["bu_prefix"]
DEFECT_CLASS_VALUE_MAP = _config["defect_class_value_map"]
REPAIR_CLASS_MAP = {k: tuple(v) for k, v in _config["repair_class_map"].items()}

# ── Constants ──────────────────────────────────────────────────────────────────

NOT_AVAILABLE = "NOT_AVAILABLE"
FXG = "FXG"
DF = "DF"
SYSDB = "SYSDB"

# CIRCUIT API config (reads from env; can be overridden at runtime)
CIRCUIT_API_ENDPOINT = os.environ.get(
    "CIRCUIT_API_ENDPOINT",
    "https://chat-ai.cisco.com/openai/deployments/gemini-3.1-flash-lite/chat/completions?api-version=2025-04-01-preview",
)
CIRCUIT_APP_KEY = os.environ.get("CIRCUIT_APP_KEY", "")
CIRCUIT_ACCESS_TOKEN = os.environ.get("CIRCUIT_ACCESS_TOKEN", "")
CIRCUIT_MODEL = os.environ.get("CIRCUIT_MODEL", "gemini-3.1-flash-lite")

# ── CIRCUIT AI API ─────────────────────────────────────────────────────────────

_classification_cache = {}


def _call_circuit_api(prompt, timeout=60):
    """Call CIRCUIT API (Cisco internal AI gateway)."""
    endpoint = CIRCUIT_API_ENDPOINT.replace("{MODEL_NAME}", CIRCUIT_MODEL)
    payload = {
        "user": json.dumps({"appkey": CIRCUIT_APP_KEY}),
        "messages": [{"role": "user", "content": prompt}],
    }
    req = request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "api-key": CIRCUIT_ACCESS_TOKEN,
        },
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {err_body or e.reason}")

    choices = data.get("choices", [])
    if choices:
        return choices[0].get("message", {}).get("content", "")
    raise RuntimeError(f"Unexpected response: {str(data)[:300]}")


def classify_failures_batch(test_step_names):
    """Use CIRCUIT AI to classify a batch of failure test step names."""
    uncached = [n for n in test_step_names if n not in _classification_cache]
    if not uncached:
        return {n: _classification_cache[n] for n in test_step_names}

    results = {}
    for i in range(0, len(uncached), 25):
        batch = uncached[i : i + 25]
        _classify_batch_chunk(batch, results)
        if i + 25 < len(uncached):
            time.sleep(1)

    _classification_cache.update(results)
    return {n: _classification_cache.get(n, _default_classification(n)) for n in test_step_names}


def _classify_batch_chunk(step_names, results):
    """Classify a chunk of <=25 step names via one API call."""
    valid_classes_str = json.dumps(DEFECT_CLASS_VALUE_MAP, indent=2)
    steps_str = "\n".join(f"  {i + 1}. {name}" for i, name in enumerate(step_names))

    prompt = f"""You are a manufacturing test failure classification expert for Cisco networking equipment.

Given the following test step names that failed during manufacturing, classify each one.

Valid defect classes and their allowed values:
{valid_classes_str}

Repair rules:
- If major_defect_class is "TEST" → repair_class="INTNC", repair_action="NONE"
- If major_defect_class is "OPERATOR_PROCESS" → repair_class="INTNC", repair_action="Repaired"
- If major_defect_class is "SOFTWARE" → repair_class="INTNC", repair_action="Repaired"
- If major_defect_class is "ORDER" → repair_class="INTNC", repair_action="Repaired"
- If major_defect_class is "HARDWARE" → repair_class="INTNC", repair_action="Repaired"

Failed test step names to classify:
{steps_str}

For each step, determine:
- major_defect_class: one of {list(DEFECT_CLASS_VALUE_MAP.keys())}
- defect_non_conform: must be a valid value from the class above
- defect_description: a brief one-line description of the likely failure cause

IMPORTANT: Respond with ONLY a JSON array. No markdown, no explanation. Example:
[
  {{"step": "BOOT_IOS", "major_defect_class": "TEST", "defect_non_conform": "NO_BOOT", "defect_description": "UUT cannot boot to IOS"}},
  ...
]"""

    for attempt in range(3):
        try:
            response = _call_circuit_api(prompt)
            parsed = _parse_json_response(response)
            if parsed:
                for item in parsed:
                    name = item.get("step", "")
                    if name in step_names:
                        cls = item.get("major_defect_class", "TEST")
                        rc, ra = REPAIR_CLASS_MAP.get(cls, ("INTNC", "NONE"))
                        results[name] = {
                            "major_defect_class": cls,
                            "defect_non_conform": item.get("defect_non_conform", "COULD_NOT_CLASSIFY"),
                            "defect_description": item.get("defect_description", ""),
                            "repair_class": rc,
                            "repair_action": ra,
                        }
                for name in step_names:
                    if name not in results:
                        results[name] = _default_classification(name)
                return
        except Exception as e:
            print(f"  [Attempt {attempt + 1}/3] AI classification error: {e}")
            if attempt < 2:
                time.sleep(5)

    for name in step_names:
        if name not in results:
            results[name] = _default_classification(name)


def _parse_json_response(text):
    """Extract JSON array from AI response text."""
    text = text.strip()
    if text.startswith("```"):
        text = "\n".join(text.split("\n")[1:])
    if text.endswith("```"):
        text = "\n".join(text.split("\n")[:-1])
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return None


def _default_classification(step_name):
    """Fallback classification when AI is unavailable."""
    return {
        "major_defect_class": "TEST",
        "defect_non_conform": "COULD_NOT_CLASSIFY",
        "defect_description": f"Auto-classified: {step_name}",
        "repair_class": "INTNC",
        "repair_action": "NONE",
    }


def bu_search(uut_type):
    """Detect BU from PID using regex prefix matching."""
    if not uut_type or (isinstance(uut_type, float) and pd.isna(uut_type)):
        return ""
    uut_type = str(uut_type).strip().upper()
    for pattern, bu in BU_PREFIX.items():
        if re.search(pattern, uut_type):
            return bu
    return ""


def reload_config(path=None):
    """Reload config from JSON (e.g. after editing transform_config.json)."""
    global BU_PREFIX, DEFECT_CLASS_VALUE_MAP, REPAIR_CLASS_MAP, _config
    _config = _load_config(path)
    BU_PREFIX = _config["bu_prefix"]
    DEFECT_CLASS_VALUE_MAP = _config["defect_class_value_map"]
    REPAIR_CLASS_MAP = {k: tuple(v) for k, v in _config["repair_class_map"].items()}


# ── Main Transform Logic ──────────────────────────────────────────────────────


def transform_excel(source_file, target_file, use_ai=True):
    """Transform source DF/DR Excel to target temp.xlsx format."""
    print(f"Reading source: {source_file}")
    df = pd.read_excel(source_file)
    total = len(df)
    print(f"Total rows: {total}")

    # Step 1: Collect unique failure step names for AI batch classification
    failure_steps = df["Test Failed At Test Area"].dropna().unique().tolist()
    failure_steps = [str(s).strip() for s in failure_steps if str(s).strip()]
    print(f"Unique failure steps: {len(failure_steps)}")

    classifications = {}
    if use_ai and CIRCUIT_ACCESS_TOKEN:
        print("Classifying failures with CIRCUIT AI...")
        classifications = classify_failures_batch(failure_steps)
        print(f"AI classified {len(classifications)} unique steps")
    else:
        if use_ai:
            print("WARNING: CIRCUIT_ACCESS_TOKEN not set. Using default classification.")
        for s in failure_steps:
            classifications[s] = _default_classification(s)

    # Step 2: Build target DataFrame
    datetime_format = "%Y-%m-%d %H:%M:%S"
    rows = []
    for _, row in df.iterrows():
        sn = str(row.get("UUT Serial Num", "")).strip()
        uudr_sn = str(row.get("UUDR Serial Num", sn)).strip()
        failed_test_area = str(row.get("Test Area", "")).strip()
        failed_test_step = str(row.get("Test Failed At Test Area", "")).strip()
        uut_part_no = str(row.get("UUT Part Num", "")).strip()
        uudr_part_no = str(row.get("UUDR Part Num", "")).strip()
        server = str(row.get("Machine", "")).strip()
        cm_location = str(row.get("CM Location", FXG)).strip()

        # Parse failed test time
        record_time_raw = row.get("Failed Test RecTime", "")
        try:
            if pd.notna(record_time_raw):
                record_time = pd.to_datetime(record_time_raw)
                fail_time_str = str(record_time + timedelta(hours=7))
            else:
                fail_time_str = ""
        except Exception:
            fail_time_str = str(record_time_raw)

        # Generate debug start/end times
        try:
            ts = pd.to_datetime(fail_time_str).timestamp()
            debug_start = pd.to_datetime(ts + random.randint(200, 400), unit="s").strftime(datetime_format)
            debug_end = pd.to_datetime(ts + random.randint(500, 600), unit="s").strftime(datetime_format)
        except Exception:
            debug_start = NOT_AVAILABLE
            debug_end = NOT_AVAILABLE

        bu = bu_search(uut_part_no)
        cls = classifications.get(failed_test_step, _default_classification(failed_test_step))

        rows.append(
            {
                "uut_serial_no": sn,
                "repair_type": DF,
                "cm_location": cm_location or FXG,
                "repair_class": cls["repair_class"],
                "reference_number": NOT_AVAILABLE,
                "failed_test": failed_test_area,
                "failed_test_time": fail_time_str,
                "uut_part_no": uut_part_no,
                "uudr_part_no": uudr_part_no if uudr_part_no and uudr_part_no != "nan" else uut_part_no,
                "uudr_serial_no": uudr_sn,
                "failure_symptom": failed_test_step,
                "debug_station": SYSDB,
                "defect_rec_cnt": NOT_AVAILABLE,
                "major_defect_class": cls["major_defect_class"],
                "defect_non_conform": cls["defect_non_conform"],
                "defect_location": NOT_AVAILABLE,
                "defect_description": cls["defect_description"],
                "cisco_component_part_number": NOT_AVAILABLE,
                "component_type": NOT_AVAILABLE,
                "component_descr": NOT_AVAILABLE,
                "component_manufacturer": NOT_AVAILABLE,
                "manufacturer_part_number": NOT_AVAILABLE,
                "component_date_code": NOT_AVAILABLE,
                "component_lotcode": NOT_AVAILABLE,
                "debug_date_time": debug_start,
                "repair_station": NOT_AVAILABLE,
                "repair_action": cls["repair_action"],
                "no_of_solder_joints": NOT_AVAILABLE,
                "repair_end_date_time": debug_end,
                "site": FXG,
                "BU": bu,
                "Server": server,
            }
        )

    result_df = pd.DataFrame(rows)
    result_df.to_excel(target_file, index=False)
    print(f"\nDone! Output written to: {target_file}")
    print(f"Total rows: {len(result_df)}")

    summary = result_df["major_defect_class"].value_counts()
    print("\n=== Classification Summary ===")
    for cls_name, count in summary.items():
        print(f"  {cls_name}: {count}")

    return result_df
