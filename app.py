import csv
import sqlite3
import threading
import time
from datetime import datetime, timezone
from io import BytesIO
from io import StringIO
from pathlib import Path
from functools import wraps

import requests
from bs4 import BeautifulSoup
from fpdf import FPDF
from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_socketio import SocketIO

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "database.db"

APP_USER = "admin"
APP_PASSWORD = "admin"
DEFAULT_SETTINGS = {
    "scrape_interval": "10",
    "alert_transportadora": "TRANSPORTADORA SEIS",
    "alert_volume": "1.0",
}

app = Flask(__name__)
app.secret_key = "change-me"
app.config["JSON_SORT_KEYS"] = False
socketio = SocketIO(app, async_mode="threading")

rate_limit_lock = threading.Lock()
rate_limit_data = {}

scrape_status = {
    "last_success": None,
    "last_error": None,
    "failures": 0,
    "response_time_ms": None,
    "online": False,
}


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS veiculos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            placa TEXT NOT NULL,
            transportadora TEXT NOT NULL,
            status_atual TEXT NOT NULL,
            timestamp_ultima_atualizacao TEXT NOT NULL
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS historico_chamados (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            id_veiculo INTEGER,
            placa TEXT NOT NULL,
            transportadora TEXT NOT NULL,
            status_anterior TEXT,
            status_novo TEXT NOT NULL,
            timestamp_mudanca TEXT NOT NULL,
            usuario_confirmacao TEXT,
            timestamp_confirmacao TEXT
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    for key, value in DEFAULT_SETTINGS.items():
        cursor.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
    conn.commit()
    conn.close()


def get_setting(key: str) -> str:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return DEFAULT_SETTINGS.get(key, "")
    return row["value"]


def set_setting(key: str, value: str) -> None:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def login_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)

    return wrapper


def is_rate_limited(ip_address: str) -> bool:
    now = time.time()
    window = 300
    limit = 5
    with rate_limit_lock:
        attempts = rate_limit_data.get(ip_address, [])
        attempts = [ts for ts in attempts if now - ts < window]
        rate_limit_data[ip_address] = attempts
        return len(attempts) >= limit


def register_failed_attempt(ip_address: str) -> None:
    with rate_limit_lock:
        rate_limit_data.setdefault(ip_address, []).append(time.time())


def normalize_text(text: str) -> str:
    return " ".join(text.strip().split())


def scrape_target():
    url = "https://agendeam.com.br/ujf/motorista.php"
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    rows = soup.find_all("tr")
    vehicles = []
    for row in rows:
        cols = [normalize_text(col.get_text(" ")) for col in row.find_all("td")]
        if len(cols) < 3:
            continue
        placa, transportadora, status = cols[0], cols[1], cols[2]
        if not placa:
            continue
        vehicles.append(
            {
                "placa": placa,
                "transportadora": transportadora,
                "status": status,
            }
        )
    return vehicles


def upsert_vehicle(conn, vehicle):
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, status_atual FROM veiculos WHERE placa = ?",
        (vehicle["placa"],),
    )
    existing = cursor.fetchone()
    timestamp = datetime.now(timezone.utc).isoformat()
    if existing:
        if existing["status_atual"] != vehicle["status"]:
            cursor.execute(
                """
                INSERT INTO historico_chamados
                (id_veiculo, placa, transportadora, status_anterior, status_novo, timestamp_mudanca)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    existing["id"],
                    vehicle["placa"],
                    vehicle["transportadora"],
                    existing["status_atual"],
                    vehicle["status"],
                    timestamp,
                ),
            )
            history_id = cursor.lastrowid
        else:
            history_id = None
        cursor.execute(
            """
            UPDATE veiculos
            SET transportadora = ?, status_atual = ?, timestamp_ultima_atualizacao = ?
            WHERE id = ?
            """,
            (
                vehicle["transportadora"],
                vehicle["status"],
                timestamp,
                existing["id"],
            ),
        )
        return existing["id"], history_id
    cursor.execute(
        """
        INSERT INTO veiculos (placa, transportadora, status_atual, timestamp_ultima_atualizacao)
        VALUES (?, ?, ?, ?)
        """,
        (
            vehicle["placa"],
            vehicle["transportadora"],
            vehicle["status"],
            timestamp,
        ),
    )
    return cursor.lastrowid, None


def broadcast_updates():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM veiculos WHERE status_atual = 'FILA'")
    fila_count = cursor.fetchone()[0]
    cursor.execute(
        """
        SELECT placa, transportadora, status_atual, timestamp_ultima_atualizacao
        FROM veiculos
        ORDER BY timestamp_ultima_atualizacao DESC
        LIMIT 50
        """
    )
    vehicles = [dict(row) for row in cursor.fetchall()]
    cursor.execute(
        """
        SELECT * FROM historico_chamados
        ORDER BY timestamp_mudanca DESC
        LIMIT 50
        """
    )
    history = [dict(row) for row in cursor.fetchall()]
    conn.close()

    socketio.emit(
        "update_data",
        {
            "fila_count": fila_count,
            "vehicles": vehicles,
            "history": history,
        },
    )


def run_scraper():
    while True:
        interval = float(get_setting("scrape_interval") or 10)
        start = time.time()
        try:
            vehicles = scrape_target()
            conn = get_db_connection()
            critical_target = normalize_text(get_setting("alert_transportadora"))
            for vehicle in vehicles:
                vehicle_id, history_id = upsert_vehicle(conn, vehicle)
                if history_id and normalize_text(vehicle["status"]) == "CHAMADO DA PORTARIA":
                    if normalize_text(vehicle["transportadora"]) == critical_target:
                        socketio.emit(
                            "critical_alert",
                            {
                                "history_id": history_id,
                                "placa": vehicle["placa"],
                                "transportadora": vehicle["transportadora"],
                                "status": vehicle["status"],
                                "volume": float(get_setting("alert_volume") or 1.0),
                            },
                        )
            conn.commit()
            conn.close()
            duration = int((time.time() - start) * 1000)
            scrape_status.update(
                {
                    "last_success": datetime.now(timezone.utc).isoformat(),
                    "last_error": None,
                    "failures": scrape_status["failures"],
                    "response_time_ms": duration,
                    "online": True,
                }
            )
            broadcast_updates()
        except Exception as exc:
            scrape_status["last_error"] = str(exc)
            scrape_status["failures"] += 1
            scrape_status["online"] = False
        time.sleep(interval)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip_address = request.remote_addr or "unknown"
        if is_rate_limited(ip_address):
            flash("Muitas tentativas. Aguarde alguns minutos.", "error")
            return render_template("login.html")
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == APP_USER and password == APP_PASSWORD:
            session["user"] = username
            return redirect(url_for("index"))
        register_failed_attempt(ip_address)
        flash("Usuário ou senha inválidos.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/monitoramento")
@login_required
def monitoramento():
    return render_template("monitoramento.html")


@app.route("/historico")
@login_required
def historico():
    return render_template("historico.html")


@app.route("/relatorios")
@login_required
def relatorios():
    return render_template("relatorios.html")


@app.route("/configuracoes", methods=["GET", "POST"])
@login_required
def configuracoes():
    if request.method == "POST":
        set_setting("scrape_interval", request.form.get("scrape_interval", "10"))
        set_setting(
            "alert_transportadora", request.form.get("alert_transportadora", "")
        )
        set_setting("alert_volume", request.form.get("alert_volume", "1.0"))
        flash("Configurações atualizadas.", "success")
        return redirect(url_for("configuracoes"))
    settings = {
        "scrape_interval": get_setting("scrape_interval"),
        "alert_transportadora": get_setting("alert_transportadora"),
        "alert_volume": get_setting("alert_volume"),
    }
    return render_template("configuracoes.html", settings=settings)


@app.route("/chamados_portaria")
@login_required
def chamados_portaria():
    return render_template("chamados_portaria.html")


@app.route("/saude")
@login_required
def saude():
    return render_template("saude.html", status=scrape_status)


@app.route("/api/veiculos")
@login_required
def api_veiculos():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT placa, transportadora, status_atual, timestamp_ultima_atualizacao
        FROM veiculos
        ORDER BY timestamp_ultima_atualizacao DESC
        """
    )
    vehicles = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify(vehicles)


@app.route("/api/historico")
@login_required
def api_historico():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT * FROM historico_chamados
        ORDER BY timestamp_mudanca DESC
        """
    )
    data = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify(data)


@app.route("/api/chamados_portaria")
@login_required
def api_chamados_portaria():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT placa, transportadora, status_atual, timestamp_ultima_atualizacao
        FROM veiculos
        WHERE status_atual = 'CHAMADO DA PORTARIA'
        ORDER BY timestamp_ultima_atualizacao DESC
        """
    )
    data = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify(data)


@app.route("/api/relatorios")
@login_required
def api_relatorios():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT transportadora, COUNT(*) as total
        FROM veiculos
        GROUP BY transportadora
        ORDER BY total DESC
        """
    )
    rows = cursor.fetchall()
    conn.close()
    return jsonify(
        {
            "labels": [row["transportadora"] for row in rows],
            "values": [row["total"] for row in rows],
        }
    )


@app.route("/api/confirm_alert", methods=["POST"])
@login_required
def api_confirm_alert():
    data = request.get_json(silent=True) or {}
    history_id = data.get("history_id")
    if not history_id:
        return jsonify({"error": "Histórico inválido."}), 400
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE historico_chamados
        SET usuario_confirmacao = ?, timestamp_confirmacao = ?
        WHERE id = ?
        """,
        (
            session.get("user"),
            datetime.now(timezone.utc).isoformat(),
            history_id,
        ),
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route("/export/chamados_portaria.csv")
@login_required
def export_chamados_csv():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT placa, transportadora, status_atual, timestamp_ultima_atualizacao
        FROM veiculos
        WHERE status_atual = 'CHAMADO DA PORTARIA'
        ORDER BY timestamp_ultima_atualizacao DESC
        """
    )
    rows = cursor.fetchall()
    conn.close()
    text_output = StringIO()
    writer = csv.writer(text_output)
    writer.writerow(["Placa", "Transportadora", "Status", "Atualizado em"])
    for row in rows:
        writer.writerow(
            [
                row["placa"],
                row["transportadora"],
                row["status_atual"],
                row["timestamp_ultima_atualizacao"],
            ]
        )
    output = BytesIO(text_output.getvalue().encode("utf-8"))
    output.seek(0)
    return send_file(
        output,
        mimetype="text/csv",
        as_attachment=True,
        download_name="chamados_portaria.csv",
    )


@app.route("/export/chamados_portaria.pdf")
@login_required
def export_chamados_pdf():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT placa, transportadora, status_atual, timestamp_ultima_atualizacao
        FROM veiculos
        WHERE status_atual = 'CHAMADO DA PORTARIA'
        ORDER BY timestamp_ultima_atualizacao DESC
        """
    )
    rows = cursor.fetchall()
    conn.close()

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.cell(0, 10, "Chamados da Portaria", ln=True)
    pdf.ln(4)
    for row in rows:
        line = (
            f"{row['placa']} | {row['transportadora']} | "
            f"{row['status_atual']} | {row['timestamp_ultima_atualizacao']}"
        )
        pdf.multi_cell(0, 8, line)
    output = BytesIO(pdf.output(dest="S").encode("latin-1"))
    output.seek(0)
    return send_file(
        output,
        mimetype="application/pdf",
        as_attachment=True,
        download_name="chamados_portaria.pdf",
    )


@socketio.on("connect")
def handle_connect():
    broadcast_updates()


if __name__ == "__main__":
    init_db()
    socketio.start_background_task(run_scraper)
    socketio.run(app, host="0.0.0.0", port=5000, debug=False)
