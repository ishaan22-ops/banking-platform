# Harbor Demo Banking

Harbor is a simulated banking application composed of six independently runnable FastAPI microservices:

| Service | Responsibility |
| --- | --- |
| API gateway | Public web UI, API routing, SEO endpoints, and security headers |
| Identity | Registration, login, password hashing, and expiring access tokens |
| Accounts | Account balances, demo deposits, and the authoritative ledger |
| Transfers | Validates and orchestrates internal transfers with retry-safe idempotency keys |
| Transaction search | Searchable, per-user transaction index with bounded pagination |
| Assistant | Signed-in support chat with optional OpenAI-backed answers |

Services communicate over private HTTP APIs. Identity, accounts, and transaction search each own a separate SQLite database and persistent volume. The account service commits both sides of each transfer atomically. The transfer service then indexes the ledger entries; if indexing is unavailable, the caller can retry with the same idempotency key without moving funds twice.

> **Demo only:** This app does not connect to banks, payment networks, or real funds. It is not suitable for real banking, customer data, or production financial use.

## Run locally with Docker Compose

Requires Docker Desktop with Docker Compose.

```powershell
docker compose up --build
```

Open <http://localhost:8000>. In local development, the identity service creates:

- Email: `demo@example.com`
- Password: `Demo123!`

New users can register from the sign-in page. The dashboard lets each user add $500 in fictional demo funds for testing transfers. Stop the services with `Ctrl+C`; run `docker compose down` to stop and remove containers while keeping the named data volumes.

To enable hosted AI responses, set `OPENAI_API_KEY` in the shell before starting Compose. Without it, the assistant uses its local FAQ responses. Only the user's question and assistant instructions are sent to OpenAI; account and transaction data are not sent. Do not share sensitive information in chat.

For development without Compose, start each module as its own process with Uvicorn and configure the service URL environment variables listed in `docker-compose.yml`. Give each service a shared internal API key and a persistent writable `DATA_DIR`.

## Features

- **Authentication:** registration, login, logout, hashed passwords, and expiring bearer sessions.
- **Accounts:** user-scoped balance views and clearly labeled simulated deposits.
- **Transfers:** atomic internal demo transfers, validation, and idempotent retries.
- **Transaction search:** searchable activity with page-size limits and user/date/type database indexes.
- **AI support:** authenticated assistant, local FAQ fallback, optional OpenAI chat completion.
- **SEO and gateway:** landing-page metadata, canonical URL, `robots.txt`, `sitemap.xml`, API routing, and security headers.

Search uses a dedicated service/database. Filters are scoped to the signed-in user and responses are paginated (up to 50 records per request).

## Container image

The root `Dockerfile` builds one shared image; each service starts a different application module. Compose provides a complete local stack. The gateway image by itself needs the other service URLs and is not a standalone banking deployment.

```powershell
docker build -t harbor-banking:latest .
```

## Kubernetes

The manifests create one Deployment per service, internal ClusterIP Services, a public LoadBalancer for the gateway, and persistent volumes for identity, accounts, and transaction search. The SQLite-backed stateful services intentionally use one replica; the gateway is horizontally replicated.

1. Build and push the shared image to a registry your cluster can access, then change each image reference in `k8s/deployment.yaml`.
2. Set `PUBLIC_BASE_URL` in the gateway deployment to the public HTTPS origin.
3. Create the internal-service key. The optional OpenAI key enables hosted AI:

   ```powershell
   kubectl create secret generic harbor-banking-secrets `
     --from-literal=internal-api-key="<long-random-value>" `
     --from-literal=openai-api-key="<optional-provider-key>"
   ```

   Omit the `openai-api-key` entry to use the local assistant fallback.
4. Apply the stack:

   ```powershell
   kubectl apply -f k8s/deployment.yaml
   kubectl get deployments,services,pods,pvc
   ```

Provision TLS for the public gateway in your cluster/load balancer setup before exposing real traffic. For a multi-replica production system, replace service SQLite databases with managed database services, implement migrations/backups/monitoring, and conduct a full security and regulatory review.

## Test

The integration tests launch all six services as separate local processes and exercise authentication, deposits, transfers, retries, search, chat, and gateway SEO:

```powershell
pip install -r requirements-dev.txt
pytest
```

## Important limitations

This is an educational demo, not a financial product. It lacks real bank integrations, KYC/AML, payment rails, account recovery, fraud prevention, regulatory controls, production-grade operational monitoring, and a production database migration strategy. Do not deploy it to handle real customer or financial data.
