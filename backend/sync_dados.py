"""
PullFlow - Robô de sincronização.

Lê arquivos CSV colocados na pasta de entrada e atualiza o PostgreSQL.
O nome do arquivo define o tipo (maiúsculas/minúsculas não importam):

  materiais*.csv  colunas: sku, descricao, canaleta_destino, estoque_minimo, ponto_pedido
                           [saldo_rack opcional]
  paletes*.csv    colunas: etiqueta, sku, endereco, quantidade, data_fab
  baixas*.csv     colunas: sku, quantidade, data_hora   [id_externo opcional]

Separador (, ou ;) e codificação (UTF-8 ou Latin-1) são detectados sozinhos.
Arquivos processados vão para <entrada>/processados/. Baixas repetidas são
ignoradas (chave única), então reenviar o mesmo relatório é seguro.

Uso (dentro de backend/):
    python sync_dados.py              # fica em loop (a cada 30s) + backup diário
    python sync_dados.py --uma-vez    # processa a pasta uma vez e sai
    python sync_dados.py --backup     # só faz o dump do banco e sai

Variáveis de ambiente:
    DATABASE_URL, PULLFLOW_ENTRADA (padrão: ../dados_entrada),
    PULLFLOW_INTERVALO (segundos, padrão 30)
"""
import hashlib
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from database import DATABASE_URL, get_conn, init_db

BASE_DIR = Path(__file__).resolve().parent.parent
ENTRADA = Path(os.getenv("PULLFLOW_ENTRADA", BASE_DIR / "dados_entrada"))
PROCESSADOS = ENTRADA / "processados"
BACKUPS = BASE_DIR / "backups"
INTERVALO = int(os.getenv("PULLFLOW_INTERVALO", "30"))
BACKUPS_MANTIDOS = 7

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("pullflow.sync")


# ----------------------------------------------------------------- leitura CSV
def ler_csv(caminho: Path) -> pd.DataFrame:
    for enc in ("utf-8-sig", "latin-1"):
        try:
            df = pd.read_csv(caminho, sep=None, engine="python", encoding=enc, dtype=str)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError(f"Não foi possível ler {caminho.name}")
    df.columns = [c.strip().lower() for c in df.columns]
    df = df.apply(lambda col: col.str.strip() if col.dtype == object else col)
    return df.dropna(how="all")


def exigir_colunas(df: pd.DataFrame, obrigatorias: list, arquivo: str):
    faltando = [c for c in obrigatorias if c not in df.columns]
    if faltando:
        raise ValueError(f"{arquivo}: faltam as colunas {faltando}")


def data(valor) -> datetime:
    return pd.to_datetime(valor, dayfirst=True).to_pydatetime()


# ---------------------------------------------------------------- processadores
def processar_materiais(df: pd.DataFrame, arquivo: str) -> int:
    exigir_colunas(
        df, ["sku", "descricao", "canaleta_destino", "estoque_minimo", "ponto_pedido"], arquivo
    )
    tem_saldo = "saldo_rack" in df.columns
    n = 0
    with get_conn() as conn:
        cur = conn.cursor()
        for r in df.itertuples(index=False):
            saldo = int(getattr(r, "saldo_rack")) if tem_saldo else 0
            cur.execute(
                """
                INSERT INTO materiais (sku, descricao, canaleta_destino, saldo_rack, estoque_minimo, ponto_pedido)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (sku) DO UPDATE SET
                    descricao = EXCLUDED.descricao,
                    canaleta_destino = EXCLUDED.canaleta_destino,
                    estoque_minimo = EXCLUDED.estoque_minimo,
                    ponto_pedido = EXCLUDED.ponto_pedido,
                    saldo_rack = CASE WHEN %s THEN EXCLUDED.saldo_rack ELSE materiais.saldo_rack END,
                    atualizado_em = NOW()
                """,
                (r.sku, r.descricao, r.canaleta_destino, saldo,
                 int(r.estoque_minimo), int(r.ponto_pedido), tem_saldo),
            )
            n += 1
    return n


def skus_existentes(cur) -> set:
    cur.execute("SELECT sku FROM materiais")
    return {row[0] for row in cur.fetchall()}


def processar_paletes(df: pd.DataFrame, arquivo: str) -> int:
    exigir_colunas(df, ["etiqueta", "sku", "endereco", "quantidade", "data_fab"], arquivo)
    n = 0
    with get_conn() as conn:
        cur = conn.cursor()
        conhecidos = skus_existentes(cur)
        for r in df.itertuples(index=False):
            if r.sku not in conhecidos:
                log.warning("%s: SKU %s não cadastrado; palete %s ignorado", arquivo, r.sku, r.etiqueta)
                continue
            cur.execute(
                """
                INSERT INTO paletes (etiqueta, sku, endereco, quantidade, data_fab)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (etiqueta) DO UPDATE SET
                    endereco = EXCLUDED.endereco,
                    quantidade = EXCLUDED.quantidade,
                    data_fab = EXCLUDED.data_fab
                WHERE paletes.status = 'DISPONIVEL'
                """,
                (r.etiqueta, r.sku, r.endereco, int(r.quantidade), data(r.data_fab)),
            )
            n += 1
    return n


def processar_baixas(df: pd.DataFrame, arquivo: str) -> int:
    exigir_colunas(df, ["sku", "quantidade", "data_hora"], arquivo)
    novas = 0
    with get_conn() as conn:
        cur = conn.cursor()
        conhecidos = skus_existentes(cur)
        for r in df.itertuples(index=False):
            if r.sku not in conhecidos:
                log.warning("%s: SKU %s não cadastrado; baixa ignorada", arquivo, r.sku)
                continue
            qtd = int(r.quantidade)
            quando = data(r.data_hora)
            id_ext = getattr(r, "id_externo", "") or ""
            chave = hashlib.md5(f"{id_ext}|{r.sku}|{qtd}|{quando.isoformat()}".encode()).hexdigest()
            cur.execute(
                """
                INSERT INTO baixas (chave, sku, quantidade, data_hora)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (chave) DO NOTHING
                RETURNING id
                """,
                (chave, r.sku, qtd, quando),
            )
            if cur.fetchone():  # só desconta do saldo se a baixa é nova
                cur.execute(
                    """
                    UPDATE materiais
                       SET saldo_rack = GREATEST(saldo_rack - %s, 0), atualizado_em = NOW()
                     WHERE sku = %s
                    """,
                    (qtd, r.sku),
                )
                novas += 1
    return novas


PROCESSADORES = [  # ordem importa: materiais -> paletes -> baixas
    ("materiais", processar_materiais),
    ("paletes", processar_paletes),
    ("baixas", processar_baixas),
]


def sincronizar_pasta():
    ENTRADA.mkdir(parents=True, exist_ok=True)
    PROCESSADOS.mkdir(parents=True, exist_ok=True)

    for prefixo, funcao in PROCESSADORES:
        for caminho in sorted(ENTRADA.glob("*.csv")):
            if not caminho.name.lower().startswith(prefixo):
                continue
            try:
                n = funcao(ler_csv(caminho), caminho.name)
                log.info("%s: %d registro(s) processado(s)", caminho.name, n)
                carimbo = datetime.now().strftime("%Y%m%d-%H%M%S")
                shutil.move(str(caminho), PROCESSADOS / f"{carimbo}_{caminho.name}")
            except Exception as e:
                log.error("Falha em %s: %s (arquivo mantido na pasta)", caminho.name, e)


# ----------------------------------------------------------------------- backup
def fazer_backup():
    BACKUPS.mkdir(exist_ok=True)
    destino = BACKUPS / f"pullflow_{datetime.now().strftime('%Y%m%d_%H%M%S')}.dump"
    try:
        subprocess.run(
            ["pg_dump", "--format=custom", "--file", str(destino), DATABASE_URL],
            check=True, capture_output=True, text=True,
        )
    except FileNotFoundError:
        log.error("pg_dump não encontrado. Instale o cliente do PostgreSQL e coloque no PATH.")
        return
    except subprocess.CalledProcessError as e:
        log.error("pg_dump falhou: %s", e.stderr.strip())
        return
    log.info("Backup criado: %s", destino.name)

    antigos = sorted(BACKUPS.glob("pullflow_*.dump"))[:-BACKUPS_MANTIDOS]
    for f in antigos:
        f.unlink()


def backup_vencido() -> bool:
    dumps = sorted(BACKUPS.glob("pullflow_*.dump"))
    return not dumps or time.time() - dumps[-1].stat().st_mtime > 24 * 3600


# ------------------------------------------------------------------------- main
if __name__ == "__main__":
    args = sys.argv[1:]

    if "--backup" in args:
        fazer_backup()
        sys.exit(0)

    init_db()

    if "--uma-vez" in args:
        sincronizar_pasta()
        sys.exit(0)

    log.info("Robô de sincronização ativo. Pasta: %s (a cada %ds)", ENTRADA, INTERVALO)
    while True:
        try:
            sincronizar_pasta()
            if backup_vencido():
                fazer_backup()
        except Exception as e:
            log.error("Erro no ciclo de sincronização: %s", e)
        time.sleep(INTERVALO)
