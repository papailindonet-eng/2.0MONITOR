import csv
import logging
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from io import BytesIO
from io import StringIO
from pathlib import Path
from functools import wraps

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import cloudscraper
except Exception:  # dependência opcional
    cloudscraper = None
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
    "scrape_url": "https://agendeam.com.br/ujf/motorista.php",
}

app = Flask(__name__)
app.secret_key = "change-me"
app.config["JSON_SORT_KEYS"] = False
socketio = SocketIO(app, async_mode="threading")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("monitoramento")

rate_limit_lock = threading.Lock()
rate_limit_data = {}

scrape_status = {
    "last_success": None,
    "last_error": None,
    "failures": 0,
    "response_time_ms": None,
    "online": False,
}


def build_http_session(use_env_proxy: bool) -> requests.Session:
    session = requests.Session()
    session.trust_env = use_env_proxy
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def fetch_html(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122 Safari/537.36",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    attempts = []

    if cloudscraper is not None:
        for use_env_proxy in (False, True):
            try:
                mode = "cloudscraper+proxy_env" if use_env_proxy else "cloudscraper+direct"
                scraper = cloudscraper.create_scraper(
                    browser={"browser": "chrome", "platform": "windows", "mobile": False}
                )
                scraper.trust_env = use_env_proxy
                scraper.headers.update(headers)
                response = scraper.get(url, timeout=25)
                response.raise_for_status()
                html = response.text or ""
                if html.strip():
                    logger.info("HTML obtido via %s", mode)
                    return html
                attempts.append(f"{mode}: resposta vazia")
            except Exception as exc:
                attempts.append(f"{mode}: {exc}")

    for use_env_proxy in (False, True):
        try:
            mode = "requests+proxy_env" if use_env_proxy else "requests+direct"
            session = build_http_session(use_env_proxy=use_env_proxy)
            response = session.get(url, timeout=25, headers=headers)
            response.raise_for_status()
            html = response.text or ""
            if html.strip():
                logger.info("HTML obtido via %s", mode)
                return html
            attempts.append(f"{mode}: resposta vazia")
        except Exception as exc:
            attempts.append(f"{mode}: {exc}")

    raise RuntimeError(
        "Falha ao acessar URL de scraping. Tentativas: " + " | ".join(attempts)
    )




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
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_veiculos_placa ON veiculos (placa)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_veiculos_status ON veiculos (status_atual)")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_historico_placa_data ON historico_chamados (placa, timestamp_mudanca)"
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


def get_scrape_interval() -> float:
    raw_value = get_setting("scrape_interval")
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        logger.warning("Valor inválido para scrape_interval (%s). Usando 10s.", raw_value)
        return 10.0
    if value < 5:
        return 5.0
    if value > 3600:
        return 3600.0
    return value


def normalize_transportadora(value: str) -> str:
    return normalize_text(value).upper()


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


def is_plate(value: str) -> bool:
    if not value:
        return False
    cleaned = re.sub(r"[^A-Z0-9]", "", value.upper())
    # Formatos aceitos:
    # - Antigo: ABC1234
    # - Mercosul: ABC1D23
    return bool(
        re.fullmatch(r"[A-Z]{3}[0-9]{4}", cleaned)
        or re.fullmatch(r"[A-Z]{3}[0-9][A-Z0-9][0-9]{2}", cleaned)
    )


def is_status(value: str) -> bool:
    status_tokens = ("FILA", "PORTARIA", "CHAMADO", "PATIO", "LIBERADO", "AGUARDANDO")
    upper_value = value.upper()
    return any(token in upper_value for token in status_tokens)


def extract_vehicle_from_cols(cols):
    placa = next((item for item in cols if is_plate(item)), None)
    if not placa:
        return None

    status = next((item for item in cols if is_status(item)), None)
    if not status:
        status = cols[-1]

    transportadora = ""
    for item in cols:
        if item != placa and item != status and len(item) > len(transportadora):
            transportadora = item

    if not transportadora:
        transportadora = "NÃO INFORMADA"

    return {
        "placa": placa.upper(),
        "transportadora": transportadora,
        "status": status,
    }


def cleanup_invalid_vehicles(conn):
    cursor = conn.cursor()
    cursor.execute("SELECT id, placa FROM veiculos ORDER BY id ASC")
    invalid_ids = []
    seen_placas = set()
    duplicated_ids = []
    for row in cursor.fetchall():
        placa = (row["placa"] or "").upper()
        if not is_plate(placa):
            invalid_ids.append(row["id"])
            continue
        if placa in seen_placas:
            duplicated_ids.append(row["id"])
            continue
        seen_placas.add(placa)

    if invalid_ids:
        cursor.executemany("DELETE FROM veiculos WHERE id = ?", [(item,) for item in invalid_ids])
        logger.info("Removidos %s registros inválidos da tabela veiculos", len(invalid_ids))
    if duplicated_ids:
        cursor.executemany("DELETE FROM veiculos WHERE id = ?", [(item,) for item in duplicated_ids])
        logger.info("Removidos %s registros duplicados por placa", len(duplicated_ids))


def parse_from_tables(soup: BeautifulSoup):
    vehicles = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        header_cells = rows[0].find_all(["th", "td"])
        headers = [normalize_text(cell.get_text(" ")).upper() for cell in header_cells]
        if not headers:
            continue

        placa_idx = next((i for i, h in enumerate(headers) if "PLACA" in h), None)
        trans_idx = next((i for i, h in enumerate(headers) if "TRANSPORT" in h or "EMPRESA" in h), None)
        status_idx = next((i for i, h in enumerate(headers) if "STATUS" in h or "SITUA" in h), None)

        if placa_idx is None:
            continue

        for row in rows[1:]:
            cols = [normalize_text(cell.get_text(" ")) for cell in row.find_all("td")]
            if not cols:
                continue
            if placa_idx >= len(cols):
                continue
            plate = cols[placa_idx]
            if not is_plate(plate):
                continue
            transportadora = cols[trans_idx] if trans_idx is not None and trans_idx < len(cols) else "NÃO INFORMADA"
            status = cols[status_idx] if status_idx is not None and status_idx < len(cols) else "NÃO INFORMADO"
            vehicles.append(
                {
                    "placa": re.sub(r"[^A-Z0-9]", "", plate.upper()),
                    "transportadora": transportadora or "NÃO INFORMADA",
                    "status": status or "NÃO INFORMADO",
                }
            )
    return vehicles


def parse_from_json_like_payload(payload: str):
    vehicles = []
    pattern = re.compile(
        r'placa["\']?\s*[:=]\s*["\'](?P<placa>[^"\']+)["\'].*?'
        r'transportadora["\']?\s*[:=]\s*["\'](?P<transportadora>[^"\']+)["\'].*?'
        r'status(?:_atual)?["\']?\s*[:=]\s*["\'](?P<status>[^"\']+)["\']',
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(payload):
        placa = normalize_text(match.group('placa')).upper()
        if not is_plate(placa):
            continue
        vehicles.append({
            'placa': placa,
            'transportadora': normalize_text(match.group('transportadora')) or 'NÃO INFORMADA',
            'status': normalize_text(match.group('status')) or 'NÃO INFORMADO',
        })
    return vehicles




def parse_from_plate_context(payload: str):
    vehicles = []
    matches = list(re.finditer(r"\b[A-Z]{3}[- ]?[0-9][A-Z0-9]?[0-9]{2,3}\b", payload, re.IGNORECASE))
    status_vocab = [
        "FILA",
        "CHAMADO DA PORTARIA",
        "AGUARDANDO",
        "LIBERADO",
        "PATIO",
        "PORTARIA",
    ]
    for match in matches:
        raw_plate = match.group(0)
        placa = re.sub(r"[^A-Z0-9]", "", raw_plate.upper())
        if not is_plate(placa):
            continue

        window_start = max(0, match.start() - 200)
        window_end = min(len(payload), match.end() + 200)
        context = payload[window_start:window_end]
        transportadora = "NÃO INFORMADA"
        status = "NÃO INFORMADO"

        for candidate in status_vocab:
            if candidate in context.upper():
                status = candidate
                break

        # Tentativa simples: pegar maior trecho textual do contexto
        text_chunks = [normalize_text(x) for x in re.split(r"[\n\r\t|;]+", context)]
        text_chunks = [x for x in text_chunks if x and x.upper() != placa and len(x) >= 5]
        if text_chunks:
            transportadora = max(text_chunks, key=len)[:120]

        vehicles.append(
            {
                "placa": placa,
                "transportadora": transportadora,
                "status": status,
            }
        )

    dedup = {}
    for item in vehicles:
        dedup[item["placa"]] = item
    return list(dedup.values())


def scrape_target():
    url = get_setting("scrape_url") or DEFAULT_SETTINGS["scrape_url"]
    html = fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")

    vehicles = parse_from_tables(soup)

    if not vehicles:
        rows = soup.find_all("tr")
        for row in rows:
            cols = [normalize_text(col.get_text(" ")) for col in row.find_all("td")]
            cols = [item for item in cols if item]
            if len(cols) < 3:
                continue
            vehicle = extract_vehicle_from_cols(cols)
            if vehicle:
                vehicles.append(vehicle)

    if not vehicles:
        vehicles = parse_from_json_like_payload(html)
    if not vehicles:
        vehicles = parse_from_plate_context(html)

    dedup = {}
    for item in vehicles:
        dedup[item["placa"]] = item

    if not dedup:
        logger.warning("Scraping retornou 0 veículos para %s", url)
    else:
        logger.info("Scraping URL %s retornou %s veículos", url, len(dedup))
    return list(dedup.values())


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
        interval = get_scrape_interval()
        start = time.time()
        conn = None
        try:
            vehicles = scrape_target()
            conn = get_db_connection()
            cleanup_invalid_vehicles(conn)
            critical_target = normalize_transportadora(get_setting("alert_transportadora"))
            for vehicle in vehicles:
                vehicle_id, history_id = upsert_vehicle(conn, vehicle)
                if history_id and normalize_text(vehicle["status"]).upper() == "CHAMADO DA PORTARIA":
                    if normalize_transportadora(vehicle["transportadora"]) == critical_target:
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
            logger.info("Ciclo de scraping concluído com %s veículos em %sms", len(vehicles), duration)
            broadcast_updates()
        except Exception as exc:
            if conn:
                conn.rollback()
            scrape_status["last_error"] = str(exc)
            scrape_status["failures"] += 1
            scrape_status["online"] = False
            logger.exception("Falha no ciclo de scraping: %s", exc)
        finally:
            if conn:
                conn.close()
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
        scrape_interval = request.form.get("scrape_interval", "10").strip()
        alert_transportadora = request.form.get("alert_transportadora", "").strip()
        alert_volume = request.form.get("alert_volume", "1.0").strip()
        scrape_url = request.form.get("scrape_url", DEFAULT_SETTINGS["scrape_url"]).strip()

        try:
            interval_value = float(scrape_interval)
            if interval_value < 5:
                interval_value = 5
        except ValueError:
            interval_value = 10

        try:
            volume_value = float(alert_volume)
            volume_value = min(max(volume_value, 0.0), 1.0)
        except ValueError:
            volume_value = 1.0

        set_setting("scrape_interval", str(interval_value))
        set_setting("alert_transportadora", alert_transportadora or DEFAULT_SETTINGS["alert_transportadora"])
        set_setting("alert_volume", str(volume_value))
        set_setting("scrape_url", scrape_url or DEFAULT_SETTINGS["scrape_url"])
        flash("Configurações atualizadas.", "success")
        return redirect(url_for("configuracoes"))
    settings = {
        "scrape_interval": get_setting("scrape_interval"),
        "alert_transportadora": get_setting("alert_transportadora"),
        "alert_volume": get_setting("alert_volume"),
        "scrape_url": get_setting("scrape_url"),
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
