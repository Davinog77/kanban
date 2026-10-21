import os
from contextlib import contextmanager
from urllib.parse import urlparse, unquote

import pymysql
from dotenv import load_dotenv
from flask import Flask, abort, redirect, render_template, request, url_for

load_dotenv()  # Carrega o arquivo .env local, se existir

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CA_PATH = os.path.join(BASE_DIR, "ca.pem")  # Certificado da Aiven (baixado no Passo 3)
STATUS_VALIDOS = ("a_fazer", "em_progresso", "concluido")


def get_db_connection():
    # Localmente, DATABASE_URL vem do arquivo .env.
    # No Render, vem da variável de ambiente cadastrada no painel (Passo 7).
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("A variável de ambiente DATABASE_URL não foi configurada!")

    # Formato da Aiven: mysql://usuario:senha@host:porta/defaultdb?ssl-mode=REQUIRED
    # O urlparse separa as partes (e o unquote trata caracteres especiais na senha).
    url = urlparse(db_url)

    if not os.path.exists(CA_PATH):
        raise ValueError("Arquivo ca.pem não encontrado na raiz do projeto (veja o Passo 3).")

    return pymysql.connect(
        host=url.hostname,
        port=url.port or 3306,
        user=unquote(url.username),
        password=unquote(url.password),
        database=url.path.lstrip("/"),
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        # Conexão criptografada, validando o certificado com o CA da Aiven
        ssl={"ca": CA_PATH, "check_hostname": False},
    )


@contextmanager
def db_cursor(commit=False):
    """Abre a conexão, entrega um cursor e garante que a conexão sempre será fechada."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            yield cursor
        if commit:
            conn.commit()
    finally:
        conn.close()


SQL_CRIAR_TABELA = """
CREATE TABLE IF NOT EXISTS tarefas (
    id INT AUTO_INCREMENT PRIMARY KEY,
    titulo VARCHAR(100) NOT NULL,
    descricao TEXT,
    materia VARCHAR(50) NOT NULL,
    status ENUM('a_fazer', 'em_progresso', 'concluido') DEFAULT 'a_fazer',
    data_criacao TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""
_tabela_verificada = False


@app.before_request
def garantir_tabela():
    """Cria a tabela 'tarefas' se ela ainda não existir (roda uma vez por processo).

    Se o banco estiver fora do ar, a exceção cai no handler abaixo e a verificação
    é tentada de novo na próxima requisição.
    """
    global _tabela_verificada
    if _tabela_verificada or request.endpoint == "static":
        return
    with db_cursor(commit=True) as cursor:
        cursor.execute(SQL_CRIAR_TABELA)  # IF NOT EXISTS: seguro rodar várias vezes
    _tabela_verificada = True


@app.errorhandler(pymysql.MySQLError)
@app.errorhandler(ValueError)
def erro_de_banco(e):
    app.logger.error("Erro de banco/configuração: %s", e)
    return (
        "<h1>Banco de dados indisponível</h1>"
        "<p>Não foi possível acessar o banco agora. Se você usa o plano gratuito da Aiven, "
        "verifique no painel se o serviço está <b>Running</b> e tente novamente.</p>",
        503,
    )


@app.route("/health")
def health():
    # Confere se o app está no ar E se o banco responde
    with db_cursor() as cursor:
        cursor.execute("SELECT 1")
    return "ok", 200


@app.route("/")
def index():
    with db_cursor() as cursor:
        cursor.execute("SELECT * FROM tarefas ORDER BY data_criacao DESC")
        tarefas = cursor.fetchall()
    return render_template("index.html", tarefas=tarefas)


@app.route("/add", methods=["POST"])
def add_task():
    titulo = request.form["titulo"].strip()
    materia = request.form["materia"].strip()
    descricao = request.form.get("descricao", "").strip()

    if not titulo or not materia:
        abort(400)

    with db_cursor(commit=True) as cursor:
        sql = "INSERT INTO tarefas (titulo, descricao, materia) VALUES (%s, %s, %s)"
        cursor.execute(sql, (titulo, descricao, materia))
    return redirect(url_for("index"))


@app.route("/update/<int:task_id>", methods=["POST"])
def update_task(task_id):
    novo_status = request.form.get("status")
    if novo_status not in STATUS_VALIDOS:
        abort(400)  # Rejeita qualquer valor fora da lista permitida

    with db_cursor(commit=True) as cursor:
        sql = "UPDATE tarefas SET status = %s WHERE id = %s"
        cursor.execute(sql, (novo_status, task_id))
    return redirect(url_for("index"))


@app.route("/delete/<int:task_id>", methods=["POST"])
def delete_task(task_id):
    with db_cursor(commit=True) as cursor:
        sql = "DELETE FROM tarefas WHERE id = %s"
        cursor.execute(sql, (task_id,))  # Um placeholder %s = um valor na tupla
    return redirect(url_for("index"))


if __name__ == "__main__":
    # Em produção o Gunicorn cuidará disso. Localmente roda em modo debug,
    # acessível apenas pelo próprio computador (127.0.0.1).
    app.run(debug=True, host="127.0.0.1", port=5000)