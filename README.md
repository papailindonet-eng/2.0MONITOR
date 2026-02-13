# Sistema de Monitoramento de Logística (versão simplificada)

Aplicação web em Flask para monitoramento de veículos com scraping em background, persistência em SQLite, atualização em tempo real via Socket.IO e alertas críticos.

## Requisitos

- Python 3.10+ (recomendado)
- `pip`
- Acesso à internet para instalar dependências

## Instalação

No diretório do projeto:

```bash
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\\Scripts\\activate     # Windows PowerShell
pip install -r requirements.txt
```


## Execucao automatica no Windows (.bat)

Se preferir, execute o arquivo abaixo com duplo clique (ou pelo Prompt):

```bat
run_monitoramento.bat
```

Ele faz automaticamente:

- deteccao do Python
- criacao do `.venv`
- instalacao das dependencias
- abertura do navegador em `http://localhost:5000/login`
- inicializacao do servidor

## Como executar

```bash
python app.py
```

O sistema sobe em:

- http://localhost:5000

Credenciais padrão:

- usuário: `admin`
- senha: `admin`

## Fluxo inicial de uso

1. Acesse `/login` e entre com as credenciais padrão.
2. O scraper inicia em background ao subir a aplicação.
3. As páginas do menu lateral recebem atualizações em tempo real.
4. Em evento crítico (transportadora configurada + `CHAMADO DA PORTARIA`), um modal bloqueante é exibido e só fecha ao confirmar.

## Configurações importantes

Na aba **Configurações** você pode alterar:

- frequência de scraping
- transportadora alvo do alerta crítico
- volume do alerta sonoro

## Banco de dados

O arquivo SQLite é criado automaticamente em `database.db` na raiz do projeto.

Backup recomendado: copie esse arquivo periodicamente.

## Observações de produção

- Em produção, utilize HTTPS (obrigatório para segurança).
- Troque `app.secret_key` e credenciais padrão antes de publicar.
- Rode atrás de um servidor apropriado (ex.: gunicorn/eventlet + reverse proxy).

