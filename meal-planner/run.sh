#!/usr/bin/env bash
set -e

# Read options from add-on config
DATA_PATH=$(python3 -c "import json; print(json.load(open('/data/options.json')).get('data_path', '/share/meal-planner'))")
API_KEY=$(python3 -c "import json; print(json.load(open('/data/options.json')).get('anthropic_api_key', ''))")

echo "Meal Planner starting..."
echo "Data directory: ${DATA_PATH}"

if [ -z "$API_KEY" ]; then
  echo "WARNING: anthropic_api_key not set — Claude features will be disabled"
else
  echo "Claude API key configured"
fi

# Create data directory if it doesn't exist
mkdir -p "${DATA_PATH}"

# Seed empty JSON files if they don't exist yet
for f in inventory.json meal-plan.json shopping-list.json status.json; do
  if [ ! -f "${DATA_PATH}/${f}" ]; then
    case "$f" in
      inventory.json)  echo '[]' > "${DATA_PATH}/${f}" ;;
      *)               echo '{}' > "${DATA_PATH}/${f}" ;;
    esac
    echo "Created empty ${f}"
  fi
done

# Seed preferences with defaults if not present
if [ ! -f "${DATA_PATH}/preferences.json" ]; then
  python3 /app/seed_preferences.py "${DATA_PATH}/preferences.json"
  echo "Created default preferences.json"
fi

export ANTHROPIC_API_KEY="$API_KEY"

echo "Starting server on port 5005..."
exec python3 /app/server.py --data-dir "${DATA_PATH}"
