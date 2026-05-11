import pandas as pd
import datetime

data_marked = pd.read_csv("results_final.csv")
print(data_marked.info())

def money_to_float(money_str):
    """
    Converts a money string matched by the regex 
    r'\$\s?[\d,]+(?:\.\d+)?(?:\s*(million|billion|thousand))?'
    into a float value.
    """
    if not money_str:
        return 0.0

    # 1. Define the multipliers
    multipliers = {
        'thousand': 1e3,
        'million': 1e6,
        'billion': 1e9
    }

    # 2. Normalize the string: lowercase and remove whitespace
    # (Removing whitespace ensures "$ 1,000" becomes "$1,000" and eventually "1000")
    clean_str = money_str.lower().replace(' ', '')

    # 3. Identify the multiplier
    multiplier = 1
    for suffix, value in multipliers.items():
        if suffix in clean_str:
            multiplier = value
            # Remove the suffix word from the string so only the number remains
            clean_str = clean_str.replace(suffix, '')
            break

    # 4. Remove currency symbols and commas to prepare for float conversion
    # Example: "$1,000.50" -> "1000.50"
    number_str = clean_str.replace('$', '').replace(',', '')

    # 5. Convert and return
    try:
        return float(number_str) * multiplier
    except ValueError:
        # Return 0 or handle the error if the format is unexpected
        return 0.0

# --- Examples ---

inputs = [
    "$500",
    "$ 1,000.50",
    "$4.5 million",
    "$1,200 thousand",
    "$2.5billion",
    "$ 100"
]

for s in inputs:
    print(f"Original: {s:<20} | Float: {money_to_float(s)}")


def main():
    # 1. URLs laden
    data_marked

if __name__ == "__main__":
    main()