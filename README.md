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

Every microservice owns a separate database:

| Service | Database |
| --- | --- |
| API gateway | PostgreSQL database `harbor_gateway` for request metadata |
| Identity | PostgreSQL database `harbor_auth` for users and sessions |
| Accounts | PostgreSQL database `harbor_accounts` for balances and the authoritative ledger |
| Transfers | PostgreSQL database `harbor_transfers` for idempotent transfer records |
| Transaction search | MongoDB database `harbor_transactions` for searchable ledger entries |
| Assistant | PostgreSQL database `harbor_assistant` for minimal chat audit metadata |

Compose runs one PostgreSQL server with a separate database, owner role, and password for each PostgreSQL service, plus a separate MongoDB server and database for transaction search. The Kubernetes deployment uses persistent PostgreSQL and MongoDB StatefulSets. Services do not share tables or database credentials. The account service commits both sides of each transfer atomically; the transfer service records successful requests and safely replays idempotent calls.

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

The Compose defaults are for local development only. Set strong values for `POSTGRES_ADMIN_PASSWORD`, each of `GATEWAY_DB_PASSWORD`, `AUTH_DB_PASSWORD`, `ACCOUNTS_DB_PASSWORD`, `TRANSFERS_DB_PASSWORD`, and `ASSISTANT_DB_PASSWORD`, as well as `MONGO_ROOT_PASSWORD`, `MONGO_APP_PASSWORD`, and `INTERNAL_API_KEY`, before using this stack outside a local machine.

## Features

- **Authentication:** registration, login, logout, hashed passwords, and expiring bearer sessions.
- **Accounts:** user-scoped balance views and clearly labeled simulated deposits.
- **Transfers:** atomic internal demo transfers, validation, and idempotent retries.
- **Transaction search:** searchable activity with page-size limits and user/date/type database indexes.
- **AI support:** authenticated assistant, local FAQ fallback, optional OpenAI chat completion.
- **SEO and gateway:** landing-page metadata, canonical URL, `robots.txt`, `sitemap.xml`, API routing, and security headers.

Search uses a dedicated service/database. Filters are scoped to the signed-in user and responses are paginated (up to 50 records per request).

## Container image

The root `Dockerfile` builds one shared application image; each service starts a different module. `postgres/Dockerfile` extends the official PostgreSQL image with initialization for the five service-owned PostgreSQL databases and roles. Compose runs an idempotent initialization job after PostgreSQL is healthy, so existing named volumes can be repaired without deleting data.

```powershell
docker build -t harbor-banking:latest .
docker build -t harbor-banking-postgres:latest .\postgres
```

## Kubernetes

The manifests create one Deployment per application service, internal ClusterIP Services, a public LoadBalancer for the gateway, a PostgreSQL StatefulSet with isolated per-service databases and roles, and a MongoDB StatefulSet for transaction search. A PostgreSQL initialization Job provisions the databases idempotently, including when a persistent volume already exists. Gateway replicas share only their own PostgreSQL database; services do not share database credentials.

1. Build and push both images to a registry your cluster can access, then change the image references in `k8s/deployment.yaml`.
2. Set `PUBLIC_BASE_URL` in the gateway deployment to the public HTTPS origin.
3. Create the internal-service key. The optional OpenAI key enables hosted AI:

   ```powershell
   kubectl create secret generic harbor-banking-secrets `
     --from-literal=internal-api-key="<long-random-value>" `
     --from-literal=postgres-admin-password="<different-long-random-password>" `
     --from-literal=gateway-db-password="<gateway-db-password>" `
     --from-literal=auth-db-password="<auth-db-password>" `
     --from-literal=accounts-db-password="<accounts-db-password>" `
     --from-literal=transfers-db-password="<transfers-db-password>" `
     --from-literal=assistant-db-password="<assistant-db-password>" `
     --from-literal=mongo-root-password="<another-long-random-password>" `
     --from-literal=mongo-app-password="<another-long-random-password>" `
     --from-literal=openai-api-key="<optional-provider-key>"
   ```

   Omit the `openai-api-key` entry to use the local assistant fallback.
4. Apply the stack:

   ```powershell
   kubectl apply -f k8s/deployment.yaml
   kubectl get deployments,services,pods,pvc
   ```

Provision TLS for the public gateway before exposing traffic. The manifest uses one PostgreSQL and one MongoDB replica for this demo; use managed, backed-up database deployments and planned migration/availability strategies before production use.

## Test

The integration tests launch all six services as separate local processes. For test isolation, the five PostgreSQL-backed service adapters use temporary SQLite files and the MongoDB adapter uses an in-memory MongoDB-compatible mock; container deployments use PostgreSQL and MongoDB as described above. Tests cover authentication, deposits, transfers, retries, search, chat, and gateway SEO:

```powershell
pip install -r requirements-dev.txt
pytest
```

## Important limitations

This is an educational demo, not a financial product. It lacks real bank integrations, KYC/AML, payment rails, account recovery, fraud prevention, regulatory controls, production-grade operational monitoring, and a production database migration strategy. Do not deploy it to handle real customer or financial data.
