# Harbor Demo Banking

Harbor is a simulated banking web application built with FastAPI. It includes account registration and sign-in, demo-only account balances and internal transfers, searchable transaction history, an assistant chatbot, and public SEO metadata/endpoints. It does **not** connect to banks, payment networks, or real funds and is not suitable for real banking or production financial use.

## Run locally

Requires Python 3.11 or newer.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open <http://localhost:8000>. In development, the app seeds a demo account:

- Email: `demo@example.com`
- Password: `Demo123!`

Set `SEED_DEMO_DATA=false` to disable the preloaded account. New accounts can be created from the sign-in page and add $500 in simulated funds from the dashboard to try transfers. All demo deposits are fictional. `DATABASE_URL` defaults to `sqlite:///./banking.db`.

### Assistant

Without an API key, the chat assistant answers common product questions locally. To enable AI answers, set `OPENAI_API_KEY` and optionally `OPENAI_MODEL` (defaults to `gpt-4o-mini`). Chat requires a signed-in account. When hosted AI is enabled, only the user's chat question and the assistant instructions are sent to OpenAI; account or transaction data is never included. Don't send sensitive information in chat. Provider errors are surfaced to the user.

## Search and SEO

Transaction history supports `q`, `limit`, and `offset` query parameters and is restricted to the signed-in user's account. Account/date and account/type indexes support the common list filters and ordering; result size is bounded. The public landing page includes a description and canonical URL. `GET /robots.txt` and `GET /sitemap.xml` are generated using `PUBLIC_BASE_URL` (defaults to `http://localhost:8000`). Set it to the externally visible HTTPS URL when deploying.

## Container

```powershell
docker build -t harbor-banking:latest .
docker run --rm -p 8000:8000 -v harbor-data:/data harbor-banking:latest
```

Set `PUBLIC_BASE_URL` to the externally visible URL. Set `OPENAI_API_KEY` only if you want to enable the hosted AI assistant.

## Kubernetes

1. Build and push the image to a registry reachable by your cluster, then update `image` in `k8s/deployment.yaml`.
2. Replace `https://bank.example.com` with the public application URL.
3. Optionally create a secret to enable hosted AI responses:

   ```powershell
   kubectl create secret generic harbor-banking-secrets `
     --from-literal=openai-api-key="<your-provider-key>"
   ```

   The secret is optional; omit it if the local FAQ assistant is sufficient.
4. Apply the manifests:

   ```powershell
   kubectl apply -f k8s/deployment.yaml
   kubectl get pods,svc,pvc
   kubectl port-forward svc/harbor-banking 8000:80
   ```

Open <http://localhost:8000> after port-forwarding. For public access, configure an Ingress or load balancer with TLS and set `PUBLIC_BASE_URL` to that HTTPS origin. The included manifest intentionally uses one replica and a persistent volume claim because the default SQLite database is single-writer and should not be shared by multiple application replicas. For a real multi-replica service, replace SQLite with a managed PostgreSQL database and plan backups, migrations, monitoring, rate limits, threat modeling, and regulatory/security review. The included readiness/liveness checks only confirm the process is responding.

## Test

```powershell
pytest
```

## Important limitations

This is an educational demo, not a financial product. It lacks real bank integrations, KYC/AML, payment rails, production session controls, account recovery, fraud prevention, regulatory controls, operational monitoring, and a production database migration strategy. Do not deploy it to handle real customer or financial data.
