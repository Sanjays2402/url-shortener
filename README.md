# minify — Serverless URL Shortener

A fully serverless URL shortener on AWS: paste a long URL into a single-page
frontend, get a short link back, and every visit is a 301 redirect with an
atomic click counter. No servers, no containers, no maintenance.

## Architecture

```mermaid
flowchart LR
    subgraph Browser
        U[User]
    end

    subgraph S3
        FE[Static frontend<br/>index.html / app.js]
    end

    subgraph "HTTP API (API Gateway v2)"
        R1[POST /shorten]
        R2[GET /{id}]
    end

    subgraph Lambda
        F1[ShortenFunction<br/>validate → mint id → write]
        F2[RedirectFunction<br/>lookup → count click → 301]
    end

    DB[(DynamoDB<br/>shortId → targetUrl<br/>clickCount)]

    U --> FE
    FE -->|fetch POST| R1
    R1 --> F1 --> DB
    U -->|visits short link| R2
    R2 --> F2 --> DB
    F2 -->|301 Location| U
```

## How it works

1. **Shorten** — `POST /shorten` with `{"url": "..."}`. The handler validates the
   URL (must be absolute `http(s)`, ≤ 2048 chars), mints a cryptographically
   random 7-char id, and writes it to DynamoDB with
   `ConditionExpression="attribute_not_exists(shortId)"` so id collisions can
   never overwrite an existing link. Returns `{"shortId", "shortUrl"}`.
2. **Redirect** — `GET /{id}` runs a single conditional `UpdateItem` that both
   verifies the id exists *and* atomically increments `clickCount`
   (`if_not_exists(clickCount, 0) + 1`), then returns a `301` with the target
   in the `Location` header. Unknown ids get a `404`.
3. **Frontend** — static files in `frontend/` served from an S3 website bucket.
   The app calls the API's `/shorten` endpoint (set `API_BASE` in `app.js` after
   deploying) and keeps a small recent-links history in `localStorage`.

## Deploy

Prerequisites: AWS CLI configured with credentials, plus the
[SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html).

```bash
cd url-shortener

# 1. Build and deploy the backend (guided on first run — accept the defaults)
sam build
sam deploy --guided

# 2. Note the ApiEndpoint output, e.g.
#    https://abc123.execute-api.us-east-1.amazonaws.com

# 3. Point the frontend at the API, then upload it
#    (edit frontend/app.js and set API_BASE to the ApiEndpoint)
BUCKET=$(aws cloudformation describe-stacks \
  --stack-name <your-stack-name> \
  --query "Stacks[0].Outputs[?OutputKey=='FrontendBucketName'].OutputValue" \
  --output text)
aws s3 sync frontend/ "s3://$BUCKET/" --delete
```

Tear down with `sam delete` when you're done experimenting.

## Free-tier cost notes

Everything here sits comfortably inside the AWS Free Tier for a portfolio
project:

| Service | Free tier (monthly) | This project |
|---|---|---|
| Lambda | 1M requests + 400k GB-seconds | 128 MB / 10 s functions, tiny usage |
| API Gateway (HTTP) | 1M API calls | a few calls per demo |
| DynamoDB | 25 GB storage, 25 WCU/RCU | on-demand, a few KB of links |
| S3 | 5 GB storage, 20k GETs | three small static files |

Beyond the free tier, cost is effectively pay-per-use pennies.

## Testing

```bash
# unit tests (mocked DynamoDB, no AWS credentials needed)
python -m pytest tests/ -v

# compile check
python -m py_compile src/shorten/app.py src/redirect/app.py

# validate the SAM template before deploying
sam validate --lint
```

End-to-end after deploying:

```bash
API="https://<api-id>.execute-api.<region>.amazonaws.com"

# create a link
curl -s -X POST "$API/shorten" \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://example.com/some/long/path"}'

# follow it (expect a 301 to the target)
curl -s -o /dev/null -w "%{http_code} -> %{redirect_url}\n" "$API/<shortId>"
```

## Enhancement ideas

- **Custom aliases** — let users request their own `/{id}` instead of a random one.
- **Link expiry** — a TTL attribute so short links self-delete after N days.
- **Per-link stats page** — expose `clickCount`/`createdAt` via `GET /stats/{id}`.
- **Rate limiting** — API Gateway usage plans or a WAF rule on `/shorten`.
- **QR codes** — render a QR for each short link in the frontend.
- **Auth** — Cognito authorizer so only you can create links (redirects stay public).

## Portfolio deliverables checklist

Capture these after deploying — they're the artifacts that make this a
portfolio piece:

- [ ] Screenshot: DynamoDB console showing a `shortId` item with `targetUrl` and `clickCount`.
- [ ] Screenshot: API Gateway HTTP API routes (`POST /shorten`, `GET /{id}`).
- [ ] Screenshot/terminal capture: `curl` creating a link and following the 301.
- [ ] Short demo GIF: paste URL → short link appears → copy → visit redirects.
- [ ] This repo pushed to GitHub with this README (architecture diagram, deploy
      steps, cost notes, tests).
- [ ] LinkedIn post: one-liner + screenshot of the frontend + repo link.
- [ ] Resume bullet, e.g. *"Built a serverless URL shortener (API Gateway,
      Lambda, DynamoDB) with atomic click analytics and a static S3 frontend —
      fully within AWS Free Tier."*
