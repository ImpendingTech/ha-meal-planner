import argparse
import asyncio
import json
import logging
import os
import tempfile
import time
import uuid
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DATA_DIR = Path(os.environ.get("DATA_DIR", "/share/meal-planner"))
APP_DIR = Path(__file__).parent
SERVER_PORT = 5005
SERVER_HOST = "0.0.0.0"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("meal-planner")

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Meal Planner Dashboard", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# In-memory response store for async Claude requests
# ---------------------------------------------------------------------------
RESPONSES: Dict[str, Dict[str, Any]] = {}
RESPONSE_TTL = 3600  # clean up after 1 hour

# Rate limiting
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX = 10
_rate_timestamps: List[float] = []

# ---------------------------------------------------------------------------
# Claude client (lazy init)
# ---------------------------------------------------------------------------
_claude_client = None
_claude_api_key: Optional[str] = None


def _load_api_key() -> str:
    """Read API key from file, then fall back to env var."""
    key_file = DATA_DIR / ".api_key"
    if key_file.exists():
        key = key_file.read_text().strip()
        if key:
            return key
    return os.environ.get("ANTHROPIC_API_KEY", "")


def _save_api_key(key: str) -> None:
    """Persist API key to file in data directory."""
    key_file = DATA_DIR / ".api_key"
    key_file.write_text(key.strip())
    # Reset client so it picks up new key
    global _claude_client, _claude_api_key
    _claude_client = None
    _claude_api_key = key.strip()
    logger.info("API key saved and client reset")


def get_claude_client():
    global _claude_client, _claude_api_key
    api_key = _load_api_key()
    if not api_key:
        return None
    # Re-init if key changed
    if api_key != _claude_api_key or _claude_client is None:
        from anthropic import Anthropic
        _claude_client = Anthropic(api_key=api_key)
        _claude_api_key = api_key
        logger.info("Claude client initialised")
    return _claude_client


# ---------------------------------------------------------------------------
# File I/O helpers
# ---------------------------------------------------------------------------
def get_file_path(filename: str) -> Path:
    return DATA_DIR / filename


def safe_read_json(filename: str, default: Any = None) -> Any:
    fp = get_file_path(filename)
    if not fp.exists():
        return default if default is not None else {}
    try:
        with open(fp, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Error reading {filename}: {e}")
        return default if default is not None else {}


def safe_write_json(filename: str, data: Any) -> None:
    fp = get_file_path(filename)
    fd, tmp = tempfile.mkstemp(dir=str(DATA_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, str(fp))
    except Exception as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise HTTPException(status_code=500, detail=f"Write error: {e}")


# ---------------------------------------------------------------------------
# Expiry helpers
# ---------------------------------------------------------------------------
def days_until(date_str: Optional[str]) -> int:
    if not date_str:
        return 999
    try:
        exp = datetime.fromisoformat(date_str).date()
        return (exp - date.today()).days
    except Exception:
        return 999


def expiry_status(date_str: Optional[str]) -> str:
    d = days_until(date_str)
    if d <= 1:
        return "red"
    if d <= 3:
        return "amber"
    return "green"


def scan_inventory_expiry() -> Dict[str, list]:
    """Scan inventory and return red/amber/green categorised items."""
    inv = safe_read_json("inventory.json", [])
    red, amber, green = [], [], []
    for item in inv:
        expiry_val = item.get("bestBefore") or item.get("expiry") or ""
        d = days_until(expiry_val)
        entry = {
            "item": item.get("name", "Unknown"),
            "amount": f"{item.get('amount', '')} {item.get('unit', '')}".strip(),
            "bestBefore": expiry_val,
            "daysUntil": d,
            "category": item.get("category", ""),
        }
        if d <= 1:
            entry["action"] = "USE TODAY — cook, eat, or freeze immediately"
            red.append(entry)
        elif d <= 3:
            entry["action"] = "Plan to use in next 1-2 meals"
            amber.append(entry)
        else:
            green.append(entry)
    return {"red": red, "amber": amber, "green": green}


# ---------------------------------------------------------------------------
# Claude system prompt + tools
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are a meal planning AI built into a Home Assistant add-on.

YOUR ROLE:
- Generate weekly meal plans with full recipes
- Create individual recipes on demand
- Build shopping lists split by delivery schedule
- Alert users to expiring ingredients and suggest usage
- Track plant diversity (30 plants/week, strict: herbs/spices = 0.25 points) and 5-a-day (5 × 80g portions daily)

RULES:
- Protein at every dinner (no prawns/shrimp)
- Day themes: Mon=Asian, Tue=Mexican, Wed=Indian, Thu=Italian, Fri=Fish, Sat/Sun=flexible
- Delivery schedule: Sunday delivery covers Mon+Tue, midweek (Tue/Wed) covers Wed-Fri
- Calorie target: ~550 kcal per dinner for 2 servings
- Indian food: traditional bold spice builds — bloom whole spices, don't hold back
- RED expiry items (<48h) MUST be prioritised in meal planning
- When generating a meal plan, include FULL recipes with ingredients (name, amount, unit) and step-by-step instructions
- Plant tracking: count every unique plant across the week. Herbs/spices = 0.25 each. Target 30+.
- 5-a-day: 5 × 80g fruit/veg portions per day. Potatoes don't count. Beans max 1 portion.

CRITICAL: You MUST use the provided tools to save your work. NEVER just describe a meal plan in text — always call update_meal_plan to save it. Similarly, always call update_shopping_list to save shopping lists. If you don't call the tools, the data won't be saved and the dashboard won't update.

When generating a meal plan, you MUST call the update_meal_plan tool with the complete plan.
When generating a shopping list, you MUST call the update_shopping_list tool.
After using tools, give a brief summary of what you saved.
"""


def build_context() -> str:
    """Build dynamic context from current JSON files."""
    prefs = safe_read_json("preferences.json", {})
    inv = safe_read_json("inventory.json", [])
    meals = safe_read_json("meal-plan.json", {})
    status = safe_read_json("status.json", {})

    # Annotate inventory with expiry status
    for item in inv:
        exp_val = item.get("bestBefore") or item.get("expiry") or ""
        item["_daysUntil"] = days_until(exp_val)
        item["_expiryStatus"] = expiry_status(exp_val)

    alerts = scan_inventory_expiry()

    return f"""
TODAY: {date.today().isoformat()} ({date.today().strftime('%A')})

EXPIRY ALERTS:
RED (use immediately): {json.dumps(alerts['red'], indent=2) if alerts['red'] else 'None'}
AMBER (use soon): {json.dumps(alerts['amber'], indent=2) if alerts['amber'] else 'None'}

CURRENT INVENTORY ({len(inv)} items):
{json.dumps(inv, indent=2)}

CURRENT MEAL PLAN:
{json.dumps(meals, indent=2) if meals else 'No meal plan yet.'}

USER PREFERENCES:
{json.dumps(prefs, indent=2) if prefs else 'No preferences set yet — use sensible defaults.'}

CURRENT STATUS:
{json.dumps(status, indent=2) if status else 'No status data yet.'}
"""


TOOLS = [
    {
        "name": "update_meal_plan",
        "description": "Write or update the weekly meal plan. Provide the complete meal plan object including weekOf, meals (keyed by day name with full recipe objects), plantTracking, and fiveADayTracking.",
        "input_schema": {
            "type": "object",
            "properties": {
                "meal_plan": {
                    "type": "object",
                    "description": "Complete meal plan object to write to meal-plan.json"
                }
            },
            "required": ["meal_plan"]
        }
    },
    {
        "name": "update_shopping_list",
        "description": "Write or update the shopping list. Provide complete shopping list with deliveries split by sunday and midweek.",
        "input_schema": {
            "type": "object",
            "properties": {
                "shopping_list": {
                    "type": "object",
                    "description": "Complete shopping list object to write to shopping-list.json"
                }
            },
            "required": ["shopping_list"]
        }
    },
    {
        "name": "update_inventory",
        "description": "Add, remove, or modify items in the storecupboard inventory. Provide the action and items.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "remove", "update", "replace_all"],
                    "description": "add=append items, remove=delete by name, update=modify existing, replace_all=overwrite entire inventory"
                },
                "items": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Array of item objects. For add/update: {name, amount, unit, category, bestBefore?, notes?}. For remove: {name}."
                }
            },
            "required": ["action", "items"]
        }
    },
    {
        "name": "update_status",
        "description": "Update the status file with expiry alerts, current week info, and meal status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "object",
                    "description": "Complete or partial status object. Will be merged with existing."
                }
            },
            "required": ["status"]
        }
    },
    {
        "name": "update_preferences",
        "description": "Update user preferences. Provide partial object to merge with existing preferences.",
        "input_schema": {
            "type": "object",
            "properties": {
                "preferences": {
                    "type": "object",
                    "description": "Partial preferences to merge"
                }
            },
            "required": ["preferences"]
        }
    },
]


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------
def execute_tool(name: str, input_data: Dict) -> Dict:
    """Execute a tool call and return result."""
    try:
        if name == "update_meal_plan":
            safe_write_json("meal-plan.json", input_data["meal_plan"])
            return {"success": True, "message": "Meal plan updated"}

        elif name == "update_shopping_list":
            safe_write_json("shopping-list.json", input_data["shopping_list"])
            return {"success": True, "message": "Shopping list updated"}

        elif name == "update_inventory":
            action = input_data["action"]
            items = input_data.get("items", [])
            inv = safe_read_json("inventory.json", [])

            if action == "add":
                for item in items:
                    if "addedDate" not in item:
                        item["addedDate"] = date.today().isoformat()
                    inv.append(item)

            elif action == "remove":
                names_to_remove = {i.get("name", "").lower() for i in items}
                inv = [i for i in inv if i.get("name", "").lower() not in names_to_remove]

            elif action == "update":
                for new_item in items:
                    found = False
                    for i, existing in enumerate(inv):
                        if existing.get("name", "").lower() == new_item.get("name", "").lower():
                            inv[i] = {**existing, **new_item}
                            found = True
                            break
                    if not found:
                        inv.append(new_item)

            elif action == "replace_all":
                inv = items

            safe_write_json("inventory.json", inv)
            return {"success": True, "message": f"Inventory {action}: {len(items)} items"}

        elif name == "update_status":
            existing = safe_read_json("status.json", {})
            existing.update(input_data["status"])
            safe_write_json("status.json", existing)
            return {"success": True, "message": "Status updated"}

        elif name == "update_preferences":
            existing = safe_read_json("preferences.json", {})
            _deep_merge(existing, input_data["preferences"])
            safe_write_json("preferences.json", existing)
            return {"success": True, "message": "Preferences updated"}

        else:
            return {"success": False, "error": f"Unknown tool: {name}"}

    except Exception as e:
        logger.error(f"Tool {name} error: {e}")
        return {"success": False, "error": str(e)}


def _deep_merge(base: dict, override: dict):
    """Recursively merge override into base."""
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


# ---------------------------------------------------------------------------
# Claude request processing
# ---------------------------------------------------------------------------
def check_rate_limit():
    now = time.time()
    _rate_timestamps[:] = [t for t in _rate_timestamps if t > now - RATE_LIMIT_WINDOW]
    if len(_rate_timestamps) >= RATE_LIMIT_MAX:
        raise HTTPException(status_code=429, detail="Rate limit exceeded — try again in a minute")
    _rate_timestamps.append(now)


def process_claude_request(response_id: str, user_message: str):
    """Call Claude with context + tools, execute tool calls, store result."""
    resp = RESPONSES[response_id]

    client = get_claude_client()
    if not client:
        resp["status"] = "error"
        resp["error"] = "Claude API key not configured. Go to Settings → Add-ons → Meal Planner → Configuration."
        return

    try:
        context = build_context()
        messages = [{"role": "user", "content": f"{context}\n\nUser request: {user_message}"}]

        # Call Claude (with retry)
        result = None
        for attempt in range(3):
            try:
                result = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=4096,
                    system=SYSTEM_PROMPT,
                    tools=TOOLS,
                    messages=messages,
                )
                break
            except Exception as e:
                if attempt == 2:
                    raise
                logger.warning(f"Claude retry {attempt+1}: {e}")
                time.sleep(2 ** attempt)

        # Process response — handle tool use loop
        tools_executed = []
        all_messages = list(messages)
        max_rounds = 5

        for _ in range(max_rounds):
            if result.stop_reason != "tool_use":
                break

            # Execute tool calls from this response
            tool_results = []
            for block in result.content:
                if block.type == "tool_use":
                    tool_result = execute_tool(block.name, block.input)
                    tools_executed.append({"tool": block.name, **tool_result})
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(tool_result),
                    })

            # Send tool results back to Claude for next round
            all_messages.append({"role": "assistant", "content": result.content})
            all_messages.append({"role": "user", "content": tool_results})

            result = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=all_messages,
            )

        # Extract final text response
        text_parts = []
        for block in result.content:
            if hasattr(block, "text"):
                text_parts.append(block.text)

        resp["claude_response"] = "\n".join(text_parts) or "Done — files updated."
        resp["tools_executed"] = tools_executed
        resp["status"] = "complete"

        # Refresh expiry alerts after any changes
        alerts = scan_inventory_expiry()
        st = safe_read_json("status.json", {})
        st["expiryAlerts"] = alerts
        st["expiryAlerts"]["lastChecked"] = date.today().isoformat()
        safe_write_json("status.json", st)

    except Exception as e:
        logger.error(f"Claude error: {e}")
        resp["status"] = "error"
        resp["error"] = str(e)


# ---------------------------------------------------------------------------
# Cleanup old responses
# ---------------------------------------------------------------------------
async def cleanup_loop():
    while True:
        now = time.time()
        expired = [k for k, v in RESPONSES.items() if now - v.get("created", 0) > RESPONSE_TTL]
        for k in expired:
            del RESPONSES[k]
        await asyncio.sleep(300)


# ---------------------------------------------------------------------------
# Background expiry scanner
# ---------------------------------------------------------------------------
async def expiry_scan_loop():
    while True:
        try:
            alerts = scan_inventory_expiry()
            st = safe_read_json("status.json", {})
            st["expiryAlerts"] = alerts
            st["expiryAlerts"]["lastChecked"] = date.today().isoformat()
            safe_write_json("status.json", st)
            logger.info(f"Expiry scan: {len(alerts['red'])} red, {len(alerts['amber'])} amber")
        except Exception as e:
            logger.error(f"Expiry scan error: {e}")
        await asyncio.sleep(6 * 3600)  # every 6 hours


@app.on_event("startup")
async def startup():
    asyncio.create_task(cleanup_loop())
    asyncio.create_task(expiry_scan_loop())
    # Run an initial expiry scan
    try:
        alerts = scan_inventory_expiry()
        st = safe_read_json("status.json", {})
        st["expiryAlerts"] = alerts
        st["expiryAlerts"]["lastChecked"] = date.today().isoformat()
        safe_write_json("status.json", st)
    except Exception:
        pass
    logger.info(f"Meal Planner started — data: {DATA_DIR}")
    if get_claude_client():
        logger.info("Claude API enabled")
    else:
        logger.warning("Claude API disabled (no key)")


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

# Health check
@app.get("/api/health")
async def health_check():
    return {
        "status": "ok",
        "data_dir": str(DATA_DIR),
        "claude_enabled": get_claude_client() is not None,
    }


# Dashboard
@app.get("/")
async def serve_dashboard():
    p = APP_DIR / "dashboard.html"
    if not p.exists():
        raise HTTPException(status_code=404, detail="dashboard.html not found")
    return FileResponse(p, media_type="text/html")


# --- Chat / AI endpoints ---

@app.post("/api/chat")
async def chat(request: Request, bg: BackgroundTasks):
    check_rate_limit()
    body = await request.json()
    message = body.get("message", "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message required")

    rid = str(uuid.uuid4())
    RESPONSES[rid] = {
        "status": "pending",
        "created": time.time(),
        "user_message": message,
        "claude_response": None,
        "tools_executed": [],
        "error": None,
    }
    bg.add_task(process_claude_request, rid, message)
    return {"response_id": rid, "status": "pending"}


@app.post("/api/action")
async def action(request: Request, bg: BackgroundTasks):
    check_rate_limit()
    body = await request.json()
    action_type = body.get("action", "")
    day = body.get("day")

    prompts = {
        "generate_meal_plan": "Generate a complete weekly meal plan for this week. Include full recipes with ingredients and step-by-step instructions for every dinner. Also include breakfast rotation, lunch suggestions, plant tracking, and 5-a-day tracking. Use the update_meal_plan tool to save it.",
        "update_shopping": "Based on the current meal plan and inventory, generate a shopping list split by Sunday and midweek deliveries. Account for what's already in stock. Use the update_shopping_list tool to save it.",
        "scan_expiry": "Scan the current inventory for expiry dates. Report what needs using urgently. Suggest which meals should use the expiring items first. Update the status with current expiry alerts using the update_status tool.",
        "create_recipe": f"Create a recipe for {day or 'today'}. It should fit the day theme, use expiring ingredients where possible, and respect all preferences. Update just that day in the meal plan using update_meal_plan.",
    }

    message = prompts.get(action_type)
    if not message:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action_type}")

    rid = str(uuid.uuid4())
    RESPONSES[rid] = {
        "status": "pending",
        "created": time.time(),
        "user_message": message,
        "claude_response": None,
        "tools_executed": [],
        "error": None,
    }
    bg.add_task(process_claude_request, rid, message)
    return {"response_id": rid, "status": "pending"}


@app.get("/api/chat/{response_id}")
async def get_chat_response(response_id: str):
    resp = RESPONSES.get(response_id)
    if not resp:
        raise HTTPException(status_code=404, detail="Response not found")
    return {
        "status": resp["status"],
        "claude_response": resp.get("claude_response"),
        "tools_executed": resp.get("tools_executed", []),
        "error": resp.get("error"),
    }


# --- Status ---
@app.get("/api/status")
async def get_status():
    return safe_read_json("status.json", {
        "expiryAlerts": {"red": [], "amber": [], "green": []},
    })


# --- Meals ---
@app.get("/api/meals")
async def get_meals():
    return safe_read_json("meal-plan.json", {"meals": {}})


@app.get("/api/meals/{day}")
async def get_meal_by_day(day: str):
    mp = safe_read_json("meal-plan.json", {"meals": {}})
    meals = mp.get("meals", {})
    if day not in meals:
        raise HTTPException(status_code=404, detail=f"No meal for '{day}'")
    return {"day": day, "meal": meals[day]}


@app.put("/api/meals/{day}/cooked")
async def mark_meal_cooked(day: str):
    mp = safe_read_json("meal-plan.json", {"meals": {}})
    meals = mp.get("meals", {})
    if day not in meals:
        raise HTTPException(status_code=404, detail=f"No meal for '{day}'")
    meal = meals[day]
    if isinstance(meal, dict):
        meal["cooked"] = True
    else:
        meals[day] = {"description": meal, "cooked": True}
    mp["meals"] = meals
    safe_write_json("meal-plan.json", mp)
    return {"day": day, "cooked": True}


@app.delete("/api/meals/{day}")
async def delete_meal(day: str):
    mp = safe_read_json("meal-plan.json", {"meals": {}})
    meals = mp.get("meals", {})
    if day not in meals:
        raise HTTPException(status_code=404, detail=f"No meal for '{day}'")
    removed = meals.pop(day)
    mp["meals"] = meals
    safe_write_json("meal-plan.json", mp)
    return {"status": "deleted", "day": day}


# --- Inventory ---
@app.get("/api/inventory")
async def get_inventory():
    return safe_read_json("inventory.json", [])


@app.post("/api/inventory")
async def add_inventory_item(request: Request):
    item = await request.json()
    required = {"name", "amount", "unit", "category"}
    if not required.issubset(item.keys()):
        raise HTTPException(status_code=400, detail=f"Missing: {required - set(item.keys())}")
    inv = safe_read_json("inventory.json", [])
    if "addedDate" not in item:
        item["addedDate"] = date.today().isoformat()
    inv.append(item)
    safe_write_json("inventory.json", inv)
    # Refresh expiry alerts
    alerts = scan_inventory_expiry()
    st = safe_read_json("status.json", {})
    st["expiryAlerts"] = {**alerts, "lastChecked": date.today().isoformat()}
    safe_write_json("status.json", st)
    return {"status": "added", "item": item, "index": len(inv) - 1}


@app.put("/api/inventory/{index}")
async def update_inventory_item(index: int, request: Request):
    item = await request.json()
    inv = safe_read_json("inventory.json", [])
    if index < 0 or index >= len(inv):
        raise HTTPException(status_code=404, detail=f"Index {index} not found")
    inv[index] = item
    safe_write_json("inventory.json", inv)
    return {"status": "updated", "item": item, "index": index}


@app.delete("/api/inventory/{index}")
async def delete_inventory_item(index: int):
    inv = safe_read_json("inventory.json", [])
    if index < 0 or index >= len(inv):
        raise HTTPException(status_code=404, detail=f"Index {index} not found")
    removed = inv.pop(index)
    safe_write_json("inventory.json", inv)
    return {"status": "deleted", "item": removed}


# --- Shopping ---
@app.get("/api/shopping")
async def get_shopping():
    return safe_read_json("shopping-list.json", {
        "deliveries": {"sunday": {"items": []}, "midweek": {"items": []}}
    })


@app.put("/api/shopping/{delivery}/{index}/purchased")
async def toggle_purchased(delivery: str, index: int):
    if delivery not in ["sunday", "midweek"]:
        raise HTTPException(status_code=400, detail=f"Invalid delivery '{delivery}'")
    sl = safe_read_json("shopping-list.json", {"deliveries": {}})
    items = sl.get("deliveries", {}).get(delivery, {}).get("items", [])
    if index < 0 or index >= len(items):
        raise HTTPException(status_code=404, detail=f"Item {index} not found")
    item = items[index]
    if isinstance(item, dict):
        item["purchased"] = not item.get("purchased", False)
    sl["deliveries"][delivery]["items"] = items
    safe_write_json("shopping-list.json", sl)
    return {"status": "toggled", "purchased": item.get("purchased", False) if isinstance(item, dict) else False}


@app.delete("/api/shopping/{delivery}/{index}")
async def delete_shopping_item(delivery: str, index: int):
    if delivery not in ["sunday", "midweek"]:
        raise HTTPException(status_code=400, detail=f"Invalid delivery '{delivery}'")
    sl = safe_read_json("shopping-list.json", {"deliveries": {}})
    items = sl.get("deliveries", {}).get(delivery, {}).get("items", [])
    if index < 0 or index >= len(items):
        raise HTTPException(status_code=404, detail=f"Item {index} not found")
    removed = items.pop(index)
    sl["deliveries"][delivery]["items"] = items
    safe_write_json("shopping-list.json", sl)
    return {"status": "deleted", "item": removed}


# --- Settings (API key) ---
@app.get("/api/settings")
async def get_settings():
    key = _load_api_key()
    return {
        "api_key_set": bool(key),
        "api_key_preview": f"{key[:10]}...{key[-4:]}" if key and len(key) > 14 else ("***" if key else ""),
        "claude_enabled": get_claude_client() is not None,
    }


@app.post("/api/settings")
async def save_settings(request: Request):
    body = await request.json()
    key = body.get("anthropic_api_key", "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="API key required")
    if not key.startswith("sk-ant-"):
        raise HTTPException(status_code=400, detail="Invalid key format — should start with sk-ant-")
    _save_api_key(key)
    return {"status": "saved", "claude_enabled": get_claude_client() is not None}


# --- Preferences ---
@app.get("/api/preferences")
async def get_preferences():
    return safe_read_json("preferences.json", {})


@app.put("/api/preferences")
async def update_preferences(request: Request):
    changes = await request.json()
    prefs = safe_read_json("preferences.json", {})
    _deep_merge(prefs, changes)
    safe_write_json("preferences.json", prefs)
    return prefs


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=str, default=None)
    parser.add_argument("--port", type=int, default=SERVER_PORT)
    args = parser.parse_args()

    if args.data_dir:
        DATA_DIR = Path(args.data_dir)

    uvicorn.run(app, host=SERVER_HOST, port=args.port, log_level="info")
