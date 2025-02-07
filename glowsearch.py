import sqlite3
import json

def get_table_name(conn):
    """
    Returns the name of the first table found in the database.
    """
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    result = cursor.fetchone()
    return result[0] if result else None

def extract_officials_info_from_json(json_str):
    """
    Given a JSON string containing a list of official objects,
    extract a list of strings formatted as:
      NAME (TITLE)
    
    In your database each official is stored as an object with keys:
      - "n": the official's name
      - "t": the official's title
    """
    try:
        officials = json.loads(json_str)
        formatted = []
        if isinstance(officials, list):
            for official in officials:
                name = official.get("n", "").strip()
                title = official.get("t", "").strip()
                if name and title:
                    formatted.append(f"{name} ({title})")
                elif name:
                    formatted.append(name)
        return formatted
    except Exception:
        return []

def format_money(value):
    """
    Converts a numeric value to a formatted string with a dollar sign and commas.
    If the conversion fails, returns the original value.
    """
    try:
        # Convert value to integer for formatting.
        val = int(value)
        return "${:,}".format(val)
    except Exception:
        return value

def search_db(db_path, search_term):
    # Connect to the SQLite database.
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row  # Enable accessing columns by name
    cursor = conn.cursor()

    # Determine the table name (assumes one table exists).
    table_name = get_table_name(conn)
    if not table_name:
        print("No tables found in the database.")
        conn.close()
        return

    # SQL query to search in filer_ein and filer_name.
    sql_query = f"""
    SELECT * FROM {table_name}
    WHERE filer_ein LIKE ? OR filer_name LIKE ?
    """
    like_term = f"%{search_term}%"
    cursor.execute(sql_query, (like_term, like_term))
    sql_matches = cursor.fetchall()

    # Also search the officials_json field by checking if any official's info contains the search term.
    cursor.execute(f"SELECT * FROM {table_name}")
    all_rows = cursor.fetchall()
    officials_matches = []
    for row in all_rows:
        officials_json = row["officials_json"] if "officials_json" in row.keys() else ""
        if not officials_json:
            continue
        officials_info = extract_officials_info_from_json(officials_json)
        # Check if any official string (e.g., "ABBY QUINN (CCRO)") contains the search term.
        for official in officials_info:
            if search_term.lower() in official.lower():
                officials_matches.append(row)
                break  # Found a match in this row; no need to check further.

    # Combine results, using filer_ein as a unique key to avoid duplicates.
    combined = {row["filer_ein"]: row for row in sql_matches + officials_matches}

    if not combined:
        print("No results found for your search.")
    else:
        print(f"\nFound {len(combined)} matching record(s):\n")
        for row in combined.values():
            print("--------------------------------------------------")
            print(f"EIN:                {row['filer_ein']}")
            print(f"Corporation Name:   {row['filer_name']}")
            print(f"Receipt Amount:     {format_money(row['receipt_amt'])}")
            print(f"Government Amount:  {format_money(row['govt_amt'])}")
            print(f"Contribution Amount:{format_money(row['contrib_amt'])}")
            print(f"Tax Year:           {row['tax_year']}")
            print("Officials:")
            officials_info = extract_officials_info_from_json(row["officials_json"])
            for info in officials_info:
                print(info)
            print("--------------------------------------------------\n")
    conn.close()

if __name__ == "__main__":
    db_path = "output.db"  # Replace with the path to your SQLite DB if needed.
    search_term = input("Enter search term: ").strip()
    if search_term:
        search_db(db_path, search_term)
    else:
        print("Please provide a valid search term.")

