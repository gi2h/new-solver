# Turnstile Solver v2.0 (Patchright)

Solver Cloudflare Turnstile berbasis Python + Patchright.

## Deploy ke Railway

1. Push folder ini ke GitHub
2. Railway -> New Project -> Deploy from GitHub
3. Railway otomatis pakai Dockerfile
4. Set env: SOLVER_KEY=kontol254535
5. Tunggu build (~5-8 menit)

## API

### Create task
POST /solve
Headers: { "key": "kontol254535", "Content-Type": "application/json" }
Body:
{
  "method": "CloudflareTask",
  "task": {
    "type": "turnstile",
    "websiteUrl": "https://target.com",
    "websiteKey": "0x4AAAAA...",
    "proxy": "http://user:pass@host:port"
  }
}

### Polling
POST /solve
Body: { "taskId": "abc123" }
