import sys
from database import get_cursor, init_db, seed_demo

init_db()
with get_cursor() as cur:
    cur.execute("TRUNCATE abastecimentos, baixas, paletes, materiais RESTART IDENTITY CASCADE")

if "limpar" in sys.argv:
    print("Banco limpo (sem dados).")
else:
    seed_demo()
    print("Dados de demonstração restaurados.")