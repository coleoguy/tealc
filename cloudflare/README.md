# tealc-aquarium Cloudflare Worker

Serves the Tealc activity feed (`tealc_activity.json`) in real time via Cloudflare KV.  
Replaces the old git-push flow (5–15 min latency) with near-instant updates (~1s).

## Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/` | none | Returns current aquarium JSON with CORS for coleoguy.github.io |
| PUT | `/` | `X-Tealc-Auth` header | Stores new JSON payload in KV |
| OPTIONS | `/` | none | CORS preflight |

## Deployed URL

```
https://tealc-aquarium.blackmon.workers.dev
```

## KV Namespace

- Binding: `AQUARIUM`
- ID: `1c2905c4edc64abb88a516512d031af7`
- Key used: `aquarium`

## Redeploy

```bash
cd "/Users/blackmon/Google Drive/My Drive/00-Lab-Agent/cloudflare"
npx wrangler deploy
```

## Rotate the secret

1. Generate a new secret:
   ```bash
   python3 -c "import secrets; print(secrets.token_hex(32))"
   ```

2. Set it as a Cloudflare secret:
   ```bash
   cd "/Users/blackmon/Google Drive/My Drive/00-Lab-Agent/cloudflare"
   echo "<new-token>" | npx wrangler secret put AQUARIUM_SECRET
   ```

3. Update `.env` in the lab agent:
   ```
   AQUARIUM_WORKER_SECRET=<new-token>
   ```

4. Restart Tealc (Chainlit + scheduler) for the new secret to take effect.

## Auth header note

Python's `urllib` gets blocked by Cloudflare bot protection unless a `User-Agent`
is set. The `_push_to_worker` function in `app.py` sends `User-Agent: Tealc-Lab-Agent/1.0`.
