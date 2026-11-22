"""
PullFlow - API FastAPI e motor de cálculo FIFO.

Regras:
  * Material com saldo_rack <= estoque_minimo  -> CRITICO
  * Material com saldo_rack <= ponto_pedido    -> ATENCAO
  * Palete sugerido = o DISPONIVEL mais antigo (data_fab) daquele SKU (FIFO)
  * Ao confirmar, só é aceito o palete FIFO; o saldo da canaleta recebe a
    quantidade do palete e o palete sai da reserva.

Rodar (dentro da pasta backend/):
    uvicorn main:app --host 0.0.0.0 --port 8000
"""
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from psycopg2 import errors
from pydantic import BaseModel, Field

from database import get_cursor, init_db

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="PullFlow API", version="1.0.0", lifespan=lifespan)

# Permite abrir o index.html direto do disco (file://) ou de outra máquina da rede
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ALERTAS_SQL = """
SELECT
    m.sku,
    m.descricao,
    m.canaleta_destino,
    m.saldo_rack,
    m.estoque_minimo,
    m.ponto_pedido,
    p.id_palete,
    p.etiqueta,
    p.endereco,
    p.quantidade,
    p.data_fab
FROM materiais m
LEFT JOIN LATERAL (
    SELECT id_palete, etiqueta, endereco, quantidade, data_fab
    FROM paletes
    WHERE paletes.sku = m.sku AND paletes.status = 'DISPONIVEL'
    ORDER BY data_fab ASC, id_palete ASC
    LIMIT 1
) p ON TRUE
WHERE m.ativo AND m.saldo_rack <= m.ponto_pedido
ORDER BY
    (m.saldo_rack <= m.estoque_minimo) DESC,
    m.saldo_rack::float / NULLIF(m.ponto_pedido, 0) ASC,
    m.sku
"""


def classificar(saldo: int, estoque_minimo: int) -> str:
    return "CRITICO" if saldo <= estoque_minimo else "ATENCAO"


@app.get("/health")
def health():
    with get_cursor() as cur:
        cur.execute("SELECT 1 AS ok")
        cur.fetchone()
    return {"status": "ok"}


@app.get("/painel-alertas")
def painel_alertas():
    """Lista os materiais que precisam de reposição, com o palete FIFO sugerido."""
    with get_cursor() as cur:
        cur.execute(ALERTAS_SQL)
        linhas = cur.fetchall()

    alertas = []
    for r in linhas:
        palete = None
        if r["id_palete"] is not None:
            palete = {
                "id_palete": r["id_palete"],
                "etiqueta": r["etiqueta"],
                "endereco": r["endereco"],
                "quantidade": r["quantidade"],
                "data_fab": r["data_fab"].isoformat(),
            }
        alertas.append(
            {
                "sku": r["sku"],
                "descricao": r["descricao"],
                "canaleta_destino": r["canaleta_destino"],
                "saldo_estimado": r["saldo_rack"],
                "status_alerta": classificar(r["saldo_rack"], r["estoque_minimo"]),
                "palete_fifo": palete,
            }
        )
    return alertas


@app.post("/confirmar-abastecimento/{sku}/{id_palete}")
def confirmar_abastecimento(sku: str, id_palete: int):
    """Registra que o operador levou o palete até a canaleta do Flow Rack."""
    with get_cursor() as cur:
        # Trava o material: duas confirmações simultâneas do mesmo SKU não se atropelam
        cur.execute("SELECT sku FROM materiais WHERE sku = %s FOR UPDATE", (sku,))
        if cur.fetchone() is None:
            raise HTTPException(404, f"SKU {sku} não encontrado.")

        cur.execute(
            """
            SELECT id_palete, quantidade FROM paletes
            WHERE sku = %s AND status = 'DISPONIVEL'
            ORDER BY data_fab ASC, id_palete ASC
            LIMIT 1
            """,
            (sku,),
        )
        fifo = cur.fetchone()
        if fifo is None:
            raise HTTPException(409, "Não há palete disponível na reserva para este SKU.")
        if fifo["id_palete"] != id_palete:
            raise HTTPException(
                409,
                "Palete fora da ordem FIFO. Atualize o painel e use o palete mais antigo.",
            )

        cur.execute(
            "UPDATE paletes SET status = 'ABASTECIDO' WHERE id_palete = %s", (id_palete,)
        )
        cur.execute(
            """
            UPDATE materiais
               SET saldo_rack = saldo_rack + %s, atualizado_em = NOW()
             WHERE sku = %s
            """,
            (fifo["quantidade"], sku),
        )
        cur.execute(
            "INSERT INTO abastecimentos (sku, id_palete, quantidade) VALUES (%s, %s, %s)",
            (sku, id_palete, fifo["quantidade"]),
        )

    return {"status": "ok", "sku": sku, "id_palete": id_palete, "quantidade": fifo["quantidade"]}


# ------------------------------------------------------------------
# Registro manual (piloto): materiais, paletes e baixas digitados na tela
# http://localhost:8000/painel/registro.html
# ------------------------------------------------------------------
class MaterialIn(BaseModel):
    sku: str = Field(min_length=1, max_length=40)
    descricao: str = Field(min_length=1, max_length=120)
    canaleta_destino: str = Field(min_length=1, max_length=80)
    saldo_rack: int = Field(default=0, ge=0)
    estoque_minimo: int = Field(default=10, ge=0)
    ponto_pedido: int = Field(default=30, ge=0)


class PaleteIn(BaseModel):
    etiqueta: str = Field(min_length=1, max_length=60)
    sku: str = Field(min_length=1, max_length=40)
    endereco: str = Field(min_length=1, max_length=80)
    quantidade: int = Field(gt=0)
    data_fab: Optional[datetime] = None


class BaixaIn(BaseModel):
    sku: str = Field(min_length=1, max_length=40)
    quantidade: int = Field(gt=0)
    data_hora: Optional[datetime] = None


@app.get("/materiais")
def listar_materiais():
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT m.sku, m.descricao, m.canaleta_destino, m.saldo_rack,
                   m.estoque_minimo, m.ponto_pedido,
                   COUNT(p.id_palete) FILTER (WHERE p.status = 'DISPONIVEL') AS paletes_disponiveis
            FROM materiais m
            LEFT JOIN paletes p ON p.sku = m.sku
            WHERE m.ativo
            GROUP BY m.sku
            ORDER BY m.sku
            """
        )
        linhas = cur.fetchall()
    for r in linhas:
        if r["saldo_rack"] <= r["ponto_pedido"]:
            r["status"] = classificar(r["saldo_rack"], r["estoque_minimo"])
        else:
            r["status"] = "OK"
    return linhas


@app.post("/materiais")
def salvar_material(m: MaterialIn):
    """Cria o material ou atualiza se o SKU já existir."""
    if m.ponto_pedido < m.estoque_minimo:
        raise HTTPException(422, "O ponto de pedido deve ser maior ou igual ao estoque mínimo.")
    sku = m.sku.strip().upper()
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO materiais (sku, descricao, canaleta_destino, saldo_rack, estoque_minimo, ponto_pedido)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (sku) DO UPDATE SET
                descricao = EXCLUDED.descricao,
                canaleta_destino = EXCLUDED.canaleta_destino,
                saldo_rack = EXCLUDED.saldo_rack,
                estoque_minimo = EXCLUDED.estoque_minimo,
                ponto_pedido = EXCLUDED.ponto_pedido,
                ativo = TRUE,
                atualizado_em = NOW()
            """,
            (sku, m.descricao.strip(), m.canaleta_destino.strip(),
             m.saldo_rack, m.estoque_minimo, m.ponto_pedido),
        )
    return {"status": "ok", "sku": sku}


@app.delete("/materiais/{sku}")
def apagar_material(sku: str):
    """Remove o material e tudo ligado a ele (paletes, baixas, abastecimentos)."""
    sku = sku.strip().upper()
    with get_cursor() as cur:
        cur.execute("DELETE FROM abastecimentos WHERE sku = %s", (sku,))
        cur.execute("DELETE FROM baixas WHERE sku = %s", (sku,))
        cur.execute("DELETE FROM paletes WHERE sku = %s", (sku,))
        cur.execute("DELETE FROM materiais WHERE sku = %s", (sku,))
        if cur.rowcount == 0:
            raise HTTPException(404, f"SKU {sku} não encontrado.")
    return {"status": "ok", "sku": sku}


@app.get("/paletes")
def listar_paletes():
    """Paletes disponíveis na reserva, do mais antigo para o mais novo em cada SKU."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT id_palete, etiqueta, sku, endereco, quantidade, data_fab
            FROM paletes
            WHERE status = 'DISPONIVEL'
            ORDER BY sku, data_fab, id_palete
            """
        )
        linhas = cur.fetchall()
    for r in linhas:
        r["data_fab"] = r["data_fab"].isoformat()
    return linhas


@app.post("/paletes")
def registrar_palete(p: PaleteIn):
    sku = p.sku.strip().upper()
    try:
        with get_cursor() as cur:
            cur.execute("SELECT 1 FROM materiais WHERE sku = %s", (sku,))
            if cur.fetchone() is None:
                raise HTTPException(404, f"SKU {sku} não cadastrado.")
            cur.execute(
                """
                INSERT INTO paletes (etiqueta, sku, endereco, quantidade, data_fab)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id_palete
                """,
                (p.etiqueta.strip().upper(), sku, p.endereco.strip().upper(),
                 p.quantidade, p.data_fab or datetime.now()),
            )
            novo = cur.fetchone()["id_palete"]
    except errors.UniqueViolation:
        raise HTTPException(409, "Já existe um palete com essa etiqueta.")
    return {"status": "ok", "id_palete": novo}


@app.post("/baixas")
def registrar_baixa(b: BaixaIn):
    """Consumo da produção: diminui o saldo da canaleta (nunca abaixo de zero)."""
    sku = b.sku.strip().upper()
    with get_cursor() as cur:
        cur.execute("SELECT saldo_rack FROM materiais WHERE sku = %s FOR UPDATE", (sku,))
        linha = cur.fetchone()
        if linha is None:
            raise HTTPException(404, f"SKU {sku} não cadastrado.")
        novo_saldo = max(linha["saldo_rack"] - b.quantidade, 0)
        cur.execute(
            "INSERT INTO baixas (chave, sku, quantidade, data_hora) VALUES (%s, %s, %s, %s)",
            (uuid.uuid4().hex, sku, b.quantidade, b.data_hora or datetime.now()),
        )
        cur.execute(
            "UPDATE materiais SET saldo_rack = %s, atualizado_em = NOW() WHERE sku = %s",
            (novo_saldo, sku),
        )
    return {"status": "ok", "sku": sku, "saldo_rack": novo_saldo}


# Painel servido também pela API: http://localhost:8000/painel/
if FRONTEND_DIR.exists():
    app.mount("/painel", StaticFiles(directory=FRONTEND_DIR, html=True), name="painel")
