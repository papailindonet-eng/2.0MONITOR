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
- inicializacao do servidor em nova janela (`monitoramento_server`)
- espera o servidor responder e so depois abre `http://localhost:5000/login`

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
- Rode atrás de um servidor apropriado (ex.: gunicorn com workers de thread/gevent + reverse proxy).



## Solucao para erro no Windows (eventlet / Python 3.13+ / 3.14)

Se aparecer erro parecido com:

- `AttributeError: module 'eventlet.green.thread' has no attribute 'start_joinable_thread'`

isso acontece por incompatibilidade do `eventlet` com versoes recentes do Python.

Este projeto agora usa `Flask-SocketIO` no modo `threading` (sem `eventlet`) para funcionar no Windows de forma mais estavel.

Se voce ja tinha um ambiente antigo, recrie a venv:

```bat
rmdir /s /q .venv
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```



## CMD com varias linhas GET /socket.io: esta correto?

Sim. Esse log e normal no Flask-SocketIO (polling/websocket).
Enquanto houver navegador conectado, o terminal mostrara requisicoes frequentes como:

- `GET /socket.io/?EIO=4...`
- `POST /socket.io/?EIO=4...`

Isso significa que a atualizacao em tempo real esta ativa.



## Se ainda "não está funcionando"

Tente este reset rápido no Windows (na pasta do projeto):

```bat
rmdir /s /q .venv
del database.db
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Depois acesse `http://localhost:5000/login`.


Você também pode ajustar a **URL de scraping** em Configurações, caso o endpoint mude.

O scraping agora tenta `cloudscraper` primeiro (quando disponível) para melhorar compatibilidade com proteções anti-bot do site principal.

O parser agora aceita placa antiga (ABC1234) e Mercosul (ABC1D23).

Conectividade: o scraper agora tenta conexão **direta e via proxy do ambiente automaticamente** (requests/cloudscraper), para funcionar tanto em redes corporativas quanto domésticas.
