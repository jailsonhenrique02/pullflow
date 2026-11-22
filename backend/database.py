"""
PullFlow - Conexão com o PostgreSQL, criação do schema e dados de demonstração.

Variável de ambiente:
    DATABASE_URL  (padrão: postgresql://postgres:postgres@localhost:5432/pullflow)

Uso direto:
    python database.py init    # cria as tabelas
    python database.py seed    # cria as tabelas + dados de demonstração
"""
import os
import sys
from contextlib import contextmanager

from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/pullflow"
)

_pool = None


def get_pool():
    global _pool
    if _pool is None:
        _pool = ThreadedConnectionPool(1, 10, dsn=DATABASE_URL)
    return _pool


@contextmanager
def get_conn():
    """Entrega uma conexão do pool. Commit no sucesso, rollback na falha."""
    pool = get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


@contextmanager
def get_cursor():
    """Cursor que devolve linhas como dicionário."""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            yield cur
        finally:
            cur.close()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS materiais (
    sku               TEXT PRIMARY KEY,
    descricao         TEXT        NOT NULL,
    canaleta_destino  TEXT        NOT NULL,
    saldo_rack        INTEGER     NOT NULL DEFAULT 0,   -- saldo estimado na canaleta (caixas)
    estoque_minimo    INTEGER     NOT NULL DEFAULT 10,  -- abaixo disso = CRÍTICO
    ponto_pedido      INTEGER     NOT NULL DEFAULT 30,  -- abaixo disso = ATENÇÃO
    ativo             BOOLEAN     NOT NULL DEFAULT TRUE,
    atualizado_em     TIMESTAMP   NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS paletes (
    id_palete   SERIAL PRIMARY KEY,
    etiqueta    TEXT      NOT NULL UNIQUE,
    sku         TEXT      NOT NULL REFERENCES materiais(sku),
    endereco    TEXT      NOT NULL,
    quantidade  INTEGER   NOT NULL,
    data_fab    TIMESTAMP NOT NULL,
    status      TEXT      NOT NULL DEFAULT 'DISPONIVEL'  -- DISPONIVEL | ABASTECIDO
);
CREATE INDEX IF NOT EXISTS idx_paletes_fifo ON paletes (sku, status, data_fab);

CREATE TABLE IF NOT EXISTS baixas (
    id         SERIAL PRIMARY KEY,
    chave      TEXT      NOT NULL UNIQUE,   -- evita contar a mesma baixa duas vezes
    sku        TEXT      NOT NULL REFERENCES materiais(sku),
    quantidade INTEGER   NOT NULL,
    data_hora  TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS abastecimentos (
    id         SERIAL PRIMARY KEY,
    sku        TEXT      NOT NULL REFERENCES materiais(sku),
    id_palete  INTEGER   NOT NULL REFERENCES paletes(id_palete),
    quantidade INTEGER   NOT NULL,
    data_hora  TIMESTAMP NOT NULL DEFAULT NOW()
);
"""


def init_db():
    with get_cursor() as cur:
        cur.execute(SCHEMA_SQL)


def seed_demo():
    """Dados de demonstração (só insere se as tabelas estiverem vazias)."""
    with get_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM materiais")
        if cur.fetchone()["n"] > 0:
            print("Banco já possui dados; seed ignorado.")
            return

        cur.execute(
            """
            INSERT INTO materiais (sku, descricao, canaleta_destino, saldo_rack, estoque_minimo, ponto_pedido)
            VALUES
              ('MAT-8842', 'CAIXA EMBALAGEM TIPO A - LINHA 01', 'FLOW RACK - CANALETA 03',  8, 10, 30),
              ('MAT-3190', 'SUPORTE PLÁSTICO INJETADO PRETO',   'FLOW RACK - CANALETA 07', 22, 10, 30),
              ('MAT-5521', 'TAMPA INJETADA CINZA',              'FLOW RACK - CANALETA 11', 70, 10, 30)
            """
        )
        cur.execute(
            """
            INSERT INTO paletes (etiqueta, sku, endereco, quantidade, data_fab)
            VALUES
              ('PLT-00492', 'MAT-8842', 'RUA B / PRÉDIO 04 / NÍVEL 1', 60, '2026-11-10 08:00'),
              ('PLT-00501', 'MAT-8842', 'RUA B / PRÉDIO 06 / NÍVEL 2', 60, '2026-11-13 09:15'),
              ('PLT-00518', 'MAT-3190', 'RUA A / PRÉDIO 12 / NÍVEL 2', 80, '2026-11-12 14:30'),
              ('PLT-00530', 'MAT-5521', 'RUA C / PRÉDIO 02 / NÍVEL 1', 50, '2026-11-14 07:40')
            """
        )
    print("Dados de demonstração criados.")


if __name__ == "__main__":
    comando = sys.argv[1] if len(sys.argv) > 1 else "init"
    init_db()
    print("Tabelas prontas.")
    if comando == "seed":
        seed_demo()
