# Meal Planner — Home Assistant Add-on

A dashboard for managing storecupboard inventory, weekly meal plans, shopping lists, and expiry alerts. Designed to work alongside Claude-powered conversational meal planning.

## Features

- **Expiry Alerts** — Red/amber/green traffic light banner, always visible
- **Tonight's Dinner** — Full recipe with ingredients, method, and "Mark as Cooked"
- **Week Overview** — 7-day grid with plant diversity and 5-a-day tracking
- **Storecupboard** — Full CRUD: add, edit, delete items grouped by category with expiry colour coding
- **Shopping List** — Sunday and midweek deliveries with tick-off

## Installation

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**
2. Click the three dots (top right) → **Repositories**
3. Add: `https://github.com/ImpendingTech/ha-meal-planner`
4. Find **Meal Planner** in the store and click **Install**
5. Start the add-on

The dashboard is available via the sidebar (Ingress) or at `http://your-ha:5005`.

## Data

JSON files are stored in `/share/meal-planner/` by default (configurable in add-on options). These are the same files Claude reads and writes:

- `inventory.json` — storecupboard items
- `meal-plan.json` — weekly meal plan with full recipes
- `shopping-list.json` — delivery-split shopping list
- `status.json` — expiry alerts and meal status
- `preferences.json` — health goals and cooking preferences

## Running Standalone

You can also run the server outside of HA:

```bash
pip install fastapi uvicorn
python3 server.py --data-dir /path/to/your/json/files
```

Then open `http://localhost:5005` in a browser.
