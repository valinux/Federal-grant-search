from flask import Flask, render_template, request
import sqlite3
import json
import urllib.parse
import re
import folium

app = Flask(__name__)
db_path = 'output_two.db'  # update with your actual DB path if needed

def build_graph_url(serialized_graph, filer_name):
    cleaned_graph = re.sub(r'[\r\n]+', '', serialized_graph).strip()
    encoded_graph = urllib.parse.quote(cleaned_graph, safe='')
    title_text = f"Charity graph\nDisplaying {filer_name}"
    encoded_title = urllib.parse.quote(title_text, safe='')
    dynamic_return_url = f"https://datarepublican.com/officers/?nonprofit_kw={urllib.parse.quote(filer_name)}"
    encoded_return_url = urllib.parse.quote(dynamic_return_url, safe='')
    final_url = (
        f"https://datarepublican.com/expose/?custom_graph={encoded_graph}"
        f"&title={encoded_title}"
        f"&return_url={encoded_return_url}"
    )
    return final_url

def build_grants_url(serialized_graph):
    cleaned_graph = re.sub(r'[\r\n]+', '', serialized_graph).strip()
    encoded_graph = urllib.parse.quote(cleaned_graph, safe='')
    final_url = f"https://datarepublican.com/expose/?eins={encoded_graph}"
    return final_url

def extract_official_names_from_json(json_str):
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

def is_phone_query(query):
    q = query.strip()
    # If it's a valid EIN, don't treat it as a phone query.
    if re.fullmatch(r'\d{9}', q) or re.fullmatch(r'\d{2}-\d{7}', q):
        return False
    # Remove common phone formatting characters.
    q_clean = re.sub(r'[\s\-\(\)]', '', q)
    if q_clean.isdigit() and 7 <= len(q_clean) <= 15:
        return True
    return False

def is_address_query(query):
    q = query.strip()
    # If the query is a valid EIN or a phone query, do not treat it as an address query.
    if re.fullmatch(r'\d{9}', q) or re.fullmatch(r'\d{2}-\d{7}', q) or is_phone_query(q):
        return False
    if re.fullmatch(r'\d{5}(-\d{4})?', q):
        return True
    if len(q) == 2 and q.isalpha():
        return True
    if any(char.isdigit() for char in q) or (',' in q) or ("PO BOX" in q.upper()):
        return True
    return False

def search_filings(query):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    q = query.strip()
    results = []
    
    if is_phone_query(q):
        # Search for phone number in ceo_home and shell_game fields.
        sql = "SELECT * FROM filings WHERE ceo_home1 LIKE ? OR shell_game LIKE ?"
        params = [f"%{q}%", f"%{q}%"]
        cursor.execute(sql, params)
        results = [dict(row) for row in cursor.fetchall()]
    
    elif is_address_query(q):
        if re.fullmatch(r'\d{5}(-\d{4})?', q):
            sql = "SELECT * FROM filings WHERE UPPER(corp_address) LIKE UPPER(?) OR UPPER(shell_game) LIKE UPPER(?)"
            params = [f"%{q}%", f"%{q}%"]
        elif len(q) == 2 and q.isalpha():
            state = q.upper()
            sql = "SELECT * FROM filings WHERE UPPER(corp_address) LIKE UPPER(?) OR UPPER(shell_game) LIKE UPPER(?)"
            params = [f"%, {state}%", f"%, {state}%"]
        else:
            words = q.upper().split()
            sql_conditions = []
            params = []
            for word in words:
                sql_conditions.append("(UPPER(corp_address) LIKE ? OR UPPER(shell_game) LIKE ?)")
                params.extend([f"%{word}%", f"%{word}%"])
            sql = "SELECT * FROM filings WHERE " + " AND ".join(sql_conditions)
        cursor.execute(sql, params)
        results = [dict(row) for row in cursor.fetchall()]
    
    else:
        like_query = f"%{q}%"
        sql = ("SELECT * FROM filings WHERE filer_ein LIKE ? OR filer_name LIKE ? "
               "OR shell_game LIKE ? OR corp_description LIKE ?")
        cursor.execute(sql, (like_query, like_query, like_query, like_query))
        sql_matches = [dict(row) for row in cursor.fetchall()]
        
        # Additionally, check within officials_json for a match.
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
        
        # Process each row: parse JSON fields and build additional URLs.
        for row in results:
            if row.get('officials_json'):
                try:
                    row['officials_json'] = json.loads(row['officials_json'])
                except Exception:
                    row['officials_json'] = []
            # Parse the ceo_home field (if present)
            if row.get('ceo_home1'):
                try:
                    row['ceo_home1'] = json.loads(row['ceo_home1'])
                except Exception:
                    row['ceo_home1'] = None
            # Parse the new shell_game field (if present)
            if row.get('shell_game'):
                try:
                    row['shell_game'] = json.loads(row['shell_game'])
                except Exception:
                    row['shell_game'] = None
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
            person_count = sum(
                1 for row in results 
                if any(query.lower() in official.get("n", "").lower() for official in row.get("officials_json", []))
            )
    
    return render_template('index.html', results=results, query=query, person_count=person_count, address_count=address_count)

@app.route('/map', methods=['GET', 'POST'])
def map_view():
    query = ""
    map_html = ""
    if request.method == 'POST':
        query = request.form.get('query', '').strip()
        results = search_filings(query)
        markers = []
        for row in results:
            # Use only the geo_loc field.
            geo_loc = (row.get('geo_loc') or '').strip()
            if geo_loc:
                try:
                    lat_str, lon_str = geo_loc.split(',')
                    lat = float(lat_str)
                    lon = float(lon_str)
                except Exception as e:
                    app.logger.error(f"Error parsing geo_loc for filer {row.get('filer_ein')}: {e}")
                    continue
                markers.append({
                    'name': row.get('filer_name', 'No Name'),
                    'address': row.get('corp_address', ''),
                    'lat': lat,
                    'lon': lon,
                    'details': f"EIN: {row.get('filer_ein', '')}, Tax Year: {row.get('tax_year', '')}"
                })
        if markers:
            # Center the map on the first marker.
            m = folium.Map(location=[markers[0]['lat'], markers[0]['lon']], zoom_start=10)
            for marker in markers:
                popup_text = f"{marker['name']}<br>{marker['address']}<br>{marker['details']}"
                folium.Marker(
                    location=[marker['lat'], marker['lon']],
                    tooltip="Click for info!",
                    popup=popup_text,
                    icon=folium.Icon(color="blue", icon="info-sign")
                ).add_to(m)
            map_html = m._repr_html_()
        else:
            map_html = "<p>No geo_loc data found for the query.</p>"
        return render_template('map.html', query=query, map_html=map_html)
    
    return render_template('map.html', query=query, map_html=map_html)

if __name__ == '__main__':
    app.run(debug=True, port=8080)

