#!/usr/bin/env bash
set -e

# Read data path from add-on options
DATA_PATH=$(python3 -c "import json; print(json.load(open('/data/options.json')).get('data_path', '/share/meal-planner'))")

echo "Meal Planner starting..."
echo "Data directory: ${DATA_PATH}"

# Create data directory if it doesn't exist
mkdir -p "${DATA_PATH}"

# Seed empty JSON files if they don't exist yet
for f in inventory.json meal-plan.json shopping-list.json status.json preferences.json; do
  if [ ! -f "${DATA_PATH}/${f}" ]; then
    case "$f" in
      inventory.json)  echo '[]' > "${DATA_PATH}/${f}" ;;
      *)               echo '{}' > "${DATA_PATH}/${f}" ;;
    esac
    echo "Created empty ${f}"
  fi
done

echo "Starting server on port 5005..."
exec python3 /app/server.py --data-dir "${DATA_PATH}"
