# New Easy to use Update Download Glowsearch2v.zip

unzip Glowsearch2v.zip

```bash
python flaskserver1.py
```

go to http://127.0.0.1:8080/

search for names or corporation accross the entire .db 
make sure there is no spacing at the end of their names for example "Mike Johnson" Not " Mike Johnson "

# Federal-grant-search

## Overview

This project sets up a Flask server, processes a database (`output.db`), and serves an HTML file (`index.html`). The server is hosted locally, and users can interact with the database via a web interface.
using the output.db file at https://drive.google.com/file/d/1RR6PPACOq5cFp4gn26ccj4bHGDskSHmf/view?usp=sharing

## Setup Instructions

Follow the steps below to set up and run the Flask server:

### 1. Download the Database

Download the `output.db` file from [this link](https://drive.google.com/file/d/1RR6PPACOq5cFp4gn26ccj4bHGDskSHmf/view?usp=sharing) and save it to your local machine.


### 2. Create Templates Directory

Create a `templates` folder to store your HTML file:

```bash
mkdir templates
```

### 4. Move `index.html`

Move the `index.html` file into the `templates` directory:

```bash
mv index.html templates/
```

### 5. Install Flask

install Flask using:

```bash
pip install Flask
```

### 6. Run the Flask Server

Start the Flask server by running:

```bash
python flaskserver.py
```

### 7. Access the Server

Open your web browser and visit the following URL:

```
http://127.0.0.1:8080/
```

### 8. Search and Find Out Who They Are

Once the server is up and running, you can interact with the database and use the application to search for and find out details about individuals or items stored in the `output.db` database.

## License

This project is licensed under the MIT License.
