"""Seed default preferences.json for first-run setup."""
import json
import sys

DEFAULT_PREFERENCES = {
    "servings": 2,
    "healthGoals": ["perimenopause", "lowCholesterol", "weightLoss", "30PlantsPerWeek"],
    "dietaryRules": {
        "proteinEveryMeal": True,
        "proteinTypes": ["meat", "fish", "poultry"],
        "avoid": ["prawns", "shrimp"]
    },
    "calorieTargets": {
        "daily": 2100,
        "deficit": 250,
        "breakdown": {
            "breakfast": 400,
            "lunch": 500,
            "dinner": 550,
            "snacks": 100,
            "drinks": 200,
            "buffer": 350
        }
    },
    "plantGoal": {
        "target": 30,
        "counting": "strict",
        "rules": {
            "vegetables": 1.0,
            "fruits": 1.0,
            "wholegrains": 1.0,
            "legumes": 1.0,
            "nuts": 1.0,
            "seeds": 1.0,
            "herbsAndSpices": 0.25
        },
        "notes": "Each unique plant = 1 full point. Herbs/spices = 0.25 each (need 4 to equal 1). Different varieties count separately."
    },
    "fiveADay": {
        "target": 5,
        "portionSize": "80g",
        "rules": "Potatoes don't count. Beans/pulses max 1 portion. Juice/smoothie max 1 portion. Dried fruit = 30g per portion."
    },
    "dayThemes": {
        "monday": "Asian",
        "tuesday": "Mexican",
        "wednesday": "Indian",
        "thursday": "Italian",
        "friday": "Fish",
        "saturday": "Flexible",
        "sunday": "Flexible"
    },
    "deliverySchedule": {
        "sunday": {
            "coversdays": ["monday", "tuesday"],
            "notes": "Sunday evening delivery — proteins and fresh produce for start of week"
        },
        "midweek": {
            "day": "Tuesday or Wednesday",
            "coversDays": ["wednesday", "thursday", "friday"],
            "notes": "Midweek delivery — proteins and fresh produce for rest of week"
        }
    },
    "cookingStyle": {
        "indian": {
            "approach": "Traditional and adventurous. Use proper spice builds — bloom whole spices in hot oil before building the dish.",
            "wholeSpices": ["mustard seeds", "cumin seeds", "fenugreek seeds", "nigella seeds", "cardamom pods", "cloves", "cinnamon stick", "bay leaves", "dried red chillies", "curry leaves", "star anise"],
            "groundSpices": ["kashmiri chilli powder", "cumin", "coriander", "turmeric", "garam masala", "amchur", "asafoetida", "fenugreek powder", "black pepper"],
            "notes": "User is well stocked on spices and wants authentic, bold flavour."
        }
    },
    "expiryRules": {
        "alertThresholds": {
            "red": "Expiring today or tomorrow — MUST cook, freeze, or eat NOW.",
            "amber": "Expiring in 2-3 days — plan to use.",
            "green": "4+ days — no action needed."
        },
        "proteinPriority": "Sort proteins by expiry date — shortest shelf life cooks first."
    },
    "defaultShelfLife": {
        "produce": 7,
        "dairy": 14,
        "protein": 3,
        "pantry": 180,
        "spices": 365,
        "canned": 730,
        "condiments": 180,
        "frozen": 90
    }
}

if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "preferences.json"
    with open(path, "w") as f:
        json.dump(DEFAULT_PREFERENCES, f, indent=2, ensure_ascii=False)
    print(f"Wrote default preferences to {path}")
