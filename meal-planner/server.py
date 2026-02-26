import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

# Configuration — set via --data-dir CLI arg or DATA_DIR env var
DATA_DIR = Path(os.environ.get("DATA_DIR", "/share/meal-planner"))
APP_DIR = Path(__file__).parent
SERVER_PORT = 5005
SERVER_HOST = "0.0.0.0"

# Initialize FastAPI app
app = FastAPI(title="Meal Planner Dashboard", version="1.0.0")

# Enable CORS (needed for HA iframe + ingress)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# HA Ingress support — rewrite base path from ingress headers
@app.middleware("http")
async def ingress_middleware(request: Request, call_next):
    """Handle HA ingress path prefix so API calls work behind the proxy."""
    response = await call_next(request)
    return response


# File I/O helpers
def get_file_path(filename: str) -> Path:
    return DATA_DIR / filename


def safe_read_json(filename: str, default: Any = None) -> Any:
    file_path = get_file_path(filename)
    if not file_path.exists():
        return default if default is not None else ({} if filename != "inventory.json" else [])
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error reading {filename}: {str(e)}"
        )


def safe_write_json(filename: str, data: Any) -> None:
    file_path = get_file_path(filename)
    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(DATA_DIR), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, str(file_path))
        except Exception as e:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise e
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error writing {filename}: {str(e)}"
        )


# Health check
@app.get("/api/health")
async def health_check() -> Dict[str, str]:
    return {"status": "ok", "data_dir": str(DATA_DIR)}


# Dashboard HTML
@app.get("/")
async def serve_dashboard():
    dashboard_path = APP_DIR / "dashboard.html"
    if not dashboard_path.exists():
        raise HTTPException(status_code=404, detail="dashboard.html not found")
    return FileResponse(dashboard_path, media_type="text/html")


# --- Status ---
@app.get("/api/status")
async def get_status() -> Dict[str, Any]:
    return safe_read_json("status.json", default={
        "expiryAlerts": {"red": [], "amber": [], "green": []},
        "currentWeek": None,
        "mealStatus": {},
        "nextWeek": None
    })


# --- Meals ---
@app.get("/api/meals")
async def get_meals() -> Dict[str, Any]:
    return safe_read_json("meal-plan.json", default={
        "weekOf": None, "plantTracking": {}, "fiveADayTracking": {},
        "breakfast": {}, "lunch": {}, "meals": {}
    })


@app.get("/api/meals/{day}")
async def get_meal_by_day(day: str) -> Dict[str, Any]:
    meal_plan = safe_read_json("meal-plan.json", default={"meals": {}})
    meals = meal_plan.get("meals", {})
    if day not in meals:
        raise HTTPException(status_code=404, detail=f"No meal for '{day}'")
    return {"day": day, "meal": meals[day]}


@app.put("/api/meals/{day}/cooked")
async def mark_meal_cooked(day: str) -> Dict[str, Any]:
    meal_plan = safe_read_json("meal-plan.json", default={"meals": {}})
    meals = meal_plan.get("meals", {})
    if day not in meals:
        raise HTTPException(status_code=404, detail=f"No meal for '{day}'")
    meal = meals[day]
    if isinstance(meal, dict):
        meal["cooked"] = True
    else:
        meals[day] = {"status": "simple", "description": meal, "cooked": True}
    meal_plan["meals"] = meals
    safe_write_json("meal-plan.json", meal_plan)
    return {"day": day, "cooked": True}


# --- Inventory ---
@app.get("/api/inventory")
async def get_inventory() -> List[Dict[str, Any]]:
    return safe_read_json("inventory.json", default=[])


@app.post("/api/inventory")
async def add_inventory_item(item: Dict[str, Any]) -> Dict[str, Any]:
    inventory = safe_read_json("inventory.json", default=[])
    required = {"name", "amount", "unit", "category"}
    if not required.issubset(item.keys()):
        raise HTTPException(status_code=400, detail=f"Missing fields: {required - set(item.keys())}")
    inventory.append(item)
    safe_write_json("inventory.json", inventory)
    return {"status": "added", "item": item, "index": len(inventory) - 1}


@app.put("/api/inventory/{index}")
async def update_inventory_item(index: int, item: Dict[str, Any]) -> Dict[str, Any]:
    inventory = safe_read_json("inventory.json", default=[])
    if index < 0 or index >= len(inventory):
        raise HTTPException(status_code=404, detail=f"Index {index} not found")
    inventory[index] = item
    safe_write_json("inventory.json", inventory)
    return {"status": "updated", "item": item, "index": index}


@app.delete("/api/inventory/{index}")
async def delete_inventory_item(index: int) -> Dict[str, Any]:
    inventory = safe_read_json("inventory.json", default=[])
    if index < 0 or index >= len(inventory):
        raise HTTPException(status_code=404, detail=f"Index {index} not found")
    removed = inventory.pop(index)
    safe_write_json("inventory.json", inventory)
    return {"status": "deleted", "item": removed, "index": index}


# --- Shopping ---
@app.get("/api/shopping")
async def get_shopping_list() -> Dict[str, Any]:
    return safe_read_json("shopping-list.json", default={
        "generatedFor": None,
        "deliveries": {
            "sunday": {"items": [], "alreadyInStock": []},
            "midweek": {"items": [], "alreadyInStock": []}
        },
        "stockNotes": ""
    })


@app.put("/api/shopping/{delivery}/{index}/purchased")
async def toggle_item_purchased(delivery: str, index: int) -> Dict[str, Any]:
    if delivery not in ["sunday", "midweek"]:
        raise HTTPException(status_code=400, detail=f"Invalid delivery '{delivery}'")
    shopping = safe_read_json("shopping-list.json", default={
        "generatedFor": None,
        "deliveries": {"sunday": {"items": []}, "midweek": {"items": []}},
    })
    items = shopping.get("deliveries", {}).get(delivery, {}).get("items", [])
    if index < 0 or index >= len(items):
        raise HTTPException(status_code=404, detail=f"Item {index} not found in '{delivery}'")
    item = items[index]
    if isinstance(item, dict):
        item["purchased"] = not item.get("purchased", False)
    shopping["deliveries"][delivery]["items"] = items
    safe_write_json("shopping-list.json", shopping)
    return {"status": "toggled", "delivery": delivery, "index": index, "purchased": item.get("purchased", False) if isinstance(item, dict) else False}


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="Meal Planner Dashboard")
    parser.add_argument("--data-dir", type=str, default=None, help="Path to JSON data files")
    parser.add_argument("--port", type=int, default=SERVER_PORT, help="Server port")
    args = parser.parse_args()

    if args.data_dir:
        DATA_DIR = Path(args.data_dir)
        print(f"Data directory: {DATA_DIR}")

    uvicorn.run(app, host=SERVER_HOST, port=args.port, log_level="info")
