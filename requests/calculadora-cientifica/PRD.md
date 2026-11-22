# PRD — `calculadora-cientifica` (Calculadora Cientifica)

## 1. Visão do Produto

- **Aplicação**: **Calculadora Cientifica** (`calculadora-cientifica`) — calculadora interativa
- **Subtítulo**: Operações básicas com histórico
- **Demanda**: Calculadora interativa web (base v1): visor de 2 linhas (expressão digitada na linha superior e resultado com prévia na linha inferior), teclado 4x5 (C, ⌫, %, ÷ | 7 8 9 × | 4 5 6 − | 1 2 3 + | ± 0 , =), suporte a teclado físico (dígitos, + - * /, %, parênteses, Enter/=, Backspace, Esc, vírgula ou ponto decimal), avaliação segura no servidor via tokenizador e shunting-yard sem eval/Function (máx 200 chars, arredondamento 12 dígitos significativos, HTTP 400 para divisão por zero ou caracteres inválidos) e painel de histórico dos últimos 20 cálculos persistido em calculadora_cientifica.calculos (iniciando vazio, clique reutiliza resultado, exclusão individual e botão Limpar).
- **Operações**: `+` `−` `×` `÷` `%` `±`
- **Isolamento de Dados**: Cloud SQL PostgreSQL compartilhado (`sdlc-shared-pgsql`), database `db_calculadora_cientifica`, schema `calculadora_cientifica`, tabela `calculadora_cientifica.calculos`.

## 2. Regras de Negócio

- **RN-001**: Expressão reavaliada no servidor por tokenizador e algoritmo shunting-yard próprios, sem execução dinâmica de código.
- **RN-002**: Aceita apenas dígitos, separador decimal (vírgula ou ponto), + − × ÷, %, parênteses e sinal negativo, com até 200 caracteres.
- **RN-003**: Divisão por zero, caracteres inválidos ou expressão incompleta retornam HTTP 400 com mensagem clara em português.
- **RN-004**: Resultado arredondado a 12 dígitos significativos (ex.: 0,1 + 0,2 = 0,3).
- **RN-005**: Histórico persistido em 'calculadora_cientifica.calculos', exibido do mais recente para o mais antigo (últimos 20).

## 3. Histórias de Usuário (Gherkin)

### US-001 — Cálculo Básico
```gherkin
Funcionalidade: Calculadora
  Cenário: Operação com precedência
    Dado que o usuário digita "2 + 3 × 4"
    Quando pressiona "=" (tela ou Enter)
    Então o visor mostra o resultado "14" e o cálculo entra no histórico
```

### US-002 — Erro Claro
```gherkin
Funcionalidade: Validação
  Cenário: Divisão por zero
    Dado que o usuário digita "10 ÷ 0"
    Quando pressiona "="
    Então o visor exibe "Divisão por zero não é permitida."
```

### US-003 — Histórico
```gherkin
Funcionalidade: Histórico
  Cenário: Reutilizar resultado
    Dado um cálculo anterior no histórico
    Quando o usuário clica no item
    Então o resultado volta ao visor para continuar a conta
```

## 4. Telas e Componentes (React 18 + TypeScript + Vite)

- **Faixa Institucional Fixa**: `"Ambiente Governado Enterprise Sandbox (ernani-demo-argolis)"`.
- **Cabeçalho**: título `Calculadora Cientifica` e indicador do modo de armazenamento.
- **Visor de 2 linhas**: expressão + resultado (prévia enquanto digita).
- **Teclado**: `0`–`9`, `,`, `+` `−` `×` `÷` `%` `±`, `C`, `⌫`, `=` + atalhos do teclado físico.
- **Histórico**: últimos 20 resultados, clique para reutilizar, `×` remove, `Limpar` apaga tudo.

## 5. Contrato REST (Node.js 22 + Cloud Run v2)

| Método | Rota | Descrição | Códigos HTTP |
|---|---|---|---|
| `GET` | `/api/health · /api/status` | Saúde do serviço, modo de armazenamento, database e schema | `200` |
| `GET` | `/api/spec` | Contrato da aplicação (título, operações e limite do histórico) | `200` |
| `POST` | `/api/calcular` | Valida e reavalia `{expressao}` no servidor e grava no histórico | `201, 400, 413` |
| `GET` | `/api/calculos?limit=` | Histórico do mais recente para o mais antigo | `200` |
| `DELETE` | `/api/calculos/:id` | Remove um cálculo do histórico | `200, 400, 404` |
| `DELETE` | `/api/calculos` | Limpa todo o histórico | `200` |

## 6. Dados

- Tabela `calculadora_cientifica.calculos` criada por `sql/001_init_and_seed.sql`; **histórico inicia vazio**.

## 7. Parâmetros de Governança no Sandbox (`ernani-demo-argolis`)

- **Orçamento Máximo (`budget_brl`)**: R$ 150.00
- **Tempo de Vida (`ttl`)**: `1h` (contado após validação do certificado HTTPS gerenciado)
- **Canais de Alerta SRE (`alert_emails`)**: `definido no manifesto de aprovação`