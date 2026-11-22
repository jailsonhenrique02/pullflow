# PullFlow

Sistema de controle e reposição puxada (Pull System) desenvolvido para agilizar o abastecimento de linhas de produção e a gestão de estoque em porta-pallets, eliminando a necessidade de sensores físicos.

## Por que este projeto foi criado?

Na rotina operacional de armazéns e linhas de produção, a falta de visibilidade em tempo real entre o consumo da linha e a reposição gera atrasos, paradas desnecessárias e falhas de comunicação entre abastecedores e operadores de empilhadeira.

O **PullFlow** nasceu da vivência prática na operação logística para resolver esse gargalo de forma simples, visual e acessível:
- **Visibilidade imediata:** Painel de alto contraste pensado para telas industriais, sinalizando rapidamente quando um item ou posição de porta-pallet precisa de reposição.
- **Comunicação direta:** Conecta o registro de consumo dos abastecedores diretamente à fila de prioridade dos operadores de empilhadeira.
- **Baixo custo de implantação:** Funciona via navegador sem depender de hardware ou sensores físicos nas estruturas.

## Tecnologias Utilizadas

- **Backend:** Python (FastAPI)
- **Banco de Dados:** PostgreSQL
- **Frontend:** HTML, CSS e JavaScript
