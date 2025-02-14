from flask import Flask, render_template, request
import sqlite3
import json
import urllib.parse
import re

app = Flask(__name__)
db_path = 'outaddresses.db'  # Change this to your actual database file

def build_graph_url(serialized_graph, filer_name):
    """
    Cleans and encodes the serialized_graph and builds the final URL.
    Uses filer_name to create a dynamic title and return_url.
    """
    cleaned_graph = re.sub(r'[\r\n]+', '', serialized_graph).strip()
    encoded_graph = urllib.parse.quote(cleaned_graph, safe='')

    # Build dynamic title and return_url
    title_text = f"Charity graph\nDisplaying {filer_name}"
    encoded_title = urllib.parse.quote(title_text, safe='')
    dynamic_return_url = f"https://datarepublican.com/officers/?nonprofit_kw={urllib.parse.quote(filer_name)}"
    encoded_return_url = urllib.parse.quote(dynamic_return_url, safe='')

    # Construct the final URL for the custom graph
    final_url = (
        f"https://datarepublican.com/expose/?custom_graph={encoded_graph}"
        f"&title={encoded_title}"
        f"&return_url={encoded_return_url}"
    )
    return final_url

def build_grants_url(serialized_graph):
    """
    Cleans and encodes the serialized_graph and builds the URL for total grants.
    """
    cleaned_graph = re.sub(r'[\r\n]+', '', serialized_graph).strip()
    encoded_graph = urllib.parse.quote(cleaned_graph, safe='')
    final_url = f"https://datarepublican.com/expose/?eins={encoded_graph}"
    return final_url

def extract_official_names_from_json(json_str):
    """
    Extracts a list of official names from a JSON string.
    """
    try:
        officials = json.loads(json_str)
        names = []
        if isinstance(officials, list):
            for official in officials:
                if isinstance(official, dict) and "n" in official:
                    names.append(official["n"])
        return names
    except Exception:
        return []

def is_address_query(query):
    """
    Determines if the query is likely an address-related query.
    Returns True if the query looks like a zip code, state code,
    or contains address elements (digits, commas, or 'PO BOX').
    """
    q = query.strip()
    # If query matches a zip code pattern (5 digits or 5+4 format)
    if re.fullmatch(r'\d{5}(-\d{4})?', q):
        return True
    # If query is exactly 2 letters, assume state code (e.g. "FL" or "SC")
    if len(q) == 2 and q.isalpha():
        return True
    # If query contains any digit, a comma, or the text "PO BOX"
    if any(char.isdigit() for char in q) or (',' in q) or ("PO BOX" in q.upper()):
        return True
    return False

def search_filings(query):
    """
    Searches the filings database based on the type of query.
    For address-related queries (full address, zip, state, or partial address),
    it searches the corp_address field using a case-insensitive match.
    For other queries (EIN, Corporation Name, Person), it searches filer_ein,
    filer_name, and scans the officials_json.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row  # enables dict-like access to rows
    cursor = conn.cursor()
    q = query.strip()
    results = []
    
    if is_address_query(q):
        # Address query handling: use UPPER() for a case-insensitive match.
        # Special case: zip code search.
        if re.fullmatch(r'\d{5}(-\d{4})?', q):
            sql = "SELECT * FROM filings WHERE UPPER(corp_address) LIKE UPPER(?)"
            params = [f"%{q}%"]
        # Special case: two-letter state code search.
        elif len(q) == 2 and q.isalpha():
            state = q.upper()
            sql = "SELECT * FROM filings WHERE UPPER(corp_address) LIKE UPPER(?)"
            params = [f"%, {state}%"]
        else:
            # For generic address queries, split the query into words and require each word to match.
            words = q.upper().split()
            sql_conditions = []
            params = []
            for word in words:
                sql_conditions.append("UPPER(corp_address) LIKE ?")
                params.append(f"%{word}%")
            sql = "SELECT * FROM filings WHERE " + " AND ".join(sql_conditions)
        cursor.execute(sql, params)
        results = [dict(row) for row in cursor.fetchall()]
    else:
        # Non-address query: search in filer_ein and filer_name.
        like_query = f"%{q}%"
        sql = "SELECT * FROM filings WHERE filer_ein LIKE ? OR filer_name LIKE ?"
        cursor.execute(sql, (like_query, like_query))
        sql_matches = [dict(row) for row in cursor.fetchall()]

        # Additionally, scan the officials_json field for person name matches.
        cursor.execute("SELECT * FROM filings")
        all_rows = [dict(row) for row in cursor.fetchall()]
        officials_matches = []
        for row in all_rows:
            officials_json = row.get("officials_json", "")
            if not officials_json:
                continue
            names = extract_official_names_from_json(officials_json)
            for name in names:
                if q.lower() in name.lower():
                    officials_matches.append(row)
                    break
        results = sql_matches + officials_matches
        # Remove duplicate results (using filer_ein as the key)
        results = list({row["filer_ein"]: row for row in results}.values())
    conn.close()
    return results

@app.route('/', methods=['GET', 'POST'])
def index():
    results = []
    query = ""
    person_count = 0
    address_count = 0
    if request.method == 'POST':
        query = request.form.get('query', '').strip()
        results = search_filings(query)
        
        # Process each result row: parse JSON fields and build additional URLs.
        for row in results:
            if row.get('officials_json'):
                try:
                    row['officials_json'] = json.loads(row['officials_json'])
                except Exception:
                    row['officials_json'] = []
            
            if row.get('serialized_graph') and row['serialized_graph'].strip():
                row['graph_url'] = build_graph_url(row['serialized_graph'], row['filer_name'])
                row['grants_url'] = build_grants_url(row['serialized_graph'])
            else:
                row['graph_url'] = None
                row['grants_url'] = None
            
            if row.get('filer_ein'):
                row['propublica_link'] = f"https://projects.propublica.org/nonprofits/organizations/{row['filer_ein']}"
            else:
                row['propublica_link'] = None
            
            if row.get('xml_name') and str(row['xml_name']).strip():
                row['propublica_link_with_xml'] = f"https://projects.propublica.org/nonprofits/organizations/{row['filer_ein']}/{row['xml_name']}/full"
            else:
                row['propublica_link_with_xml'] = None
        
        if is_address_query(query):
            address_count = len(results)
        else:
            # Count corporations where the person's name is found in officials_json.
            person_count = sum(
                1 for row in results 
                if any(query.lower() in official.get("n", "").lower() for official in row.get("officials_json", []))
            )

    return render_template('index.html', results=results, query=query, person_count=person_count, address_count=address_count)

if __name__ == '__main__':
    app.run(debug=True, port=8080)

