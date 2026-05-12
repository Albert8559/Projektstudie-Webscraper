import pandas as pd

def money_to_float(money_str):
    """
    Converts a money string into a float value.
    Handles: $, commas, spaces, and multipliers (million, billion, thousand).
    """
    # Check for NaN or None/Empty values
    if pd.isna(money_str) or not money_str:
        return 0.0

    # 1. Define the multipliers
    multipliers = {
        'thousand': 1e3,
        'million': 1e6,
        'billion': 1e9
    }

    # 2. Normalize the string: lowercase and remove whitespace
    clean_str = str(money_str).lower().replace(' ', '')

    # 3. Identify the multiplier
    multiplier = 1
    for suffix, value in multipliers.items():
        if suffix in clean_str:
            multiplier = value
            clean_str = clean_str.replace(suffix, '')
            break

    # 4. Remove currency symbols and commas
    number_str = clean_str.replace('$', '').replace(',', '')

    # 5. Convert and return
    try:
        return float(number_str) * multiplier
    except ValueError:
        return 0.0

def main():
    input_filename = "results_final_comp_3.csv"
    output_filename = "results_final_comp_3_cleaned.csv"
    
    print(f"Loading data from {input_filename}...")
    try:
        df = pd.read_csv(input_filename)
    except FileNotFoundError:
        print(f"Error: The file '{input_filename}' was not found.")
        return

    # 1. Convert payment_amount to float
    print("Converting payment_amount values...")
    df['payment_amount'] = df['payment_amount'].apply(money_to_float)

    # 2. Ensure payment_found is strictly an integer (0 or 1)
    # This prevents it from becoming 0.0 or 1.0 in the CSV
    if 'payment_found' in df.columns:
        # We use 'Int64' (capital I) to handle potential NaNs if any existed, 
        # otherwise standard 'int' works. Here we force to nullable integer.
        df['payment_found'] = df['payment_found'].astype('Int64')
        print("'payment_found' column ensured as integer.")
    else:
        print("Warning: 'payment_found' column was not found in the CSV.")

    # 3. Save the updated dataframe to a new CSV file
    # float_format='%.2f' ensures large numbers are written as "11400000.00" 
    # instead of scientific notation like "1.14e+07"
    df.to_csv(output_filename, index=False, float_format='%.2f')
    
    print(f"Success! Cleaned data saved to {output_filename}")
    
    # Optional: Print a preview to verify
    print("\nPreview of data types:")
    print(df[['payment_found', 'payment_amount']].dtypes)
    print("\nPreview of rows:")
    print(df[['case_name', 'payment_found', 'payment_amount']].head())

if __name__ == "__main__":
    main()