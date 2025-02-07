from flask import Flask, render_template, request
import sqlite3
import json

app = Flask(__name__)

db_path = 'output.db'  # Change this to your actual database file


def search_filings(query):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    sql = """
    SELECT * FROM filings
    WHERE filer_ein = ? OR filer_name LIKE ?
    """
    cursor.execute(sql, (query, f'%{query}%'))
    results = cursor.fetchall()
    conn.close()

    return [dict(row) for row in results]


@app.route('/', methods=['GET', 'POST'])
def index():
    results = []
    query = ""
    if request.method == 'POST':
        query = request.form['query']
        results = search_filings(query)
        
        for row in results:
            if row.get('officials_json'):
                row['officials_json'] = json.loads(row['officials_json'])
    
    return render_template('index.html', results=results, query=query)


if __name__ == '__main__':
    app.run(debug=True, port=8080)
