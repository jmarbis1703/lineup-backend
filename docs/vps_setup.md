# VPS Setup Guide — Ubuntu 22.04

Step-by-step bootstrap for a fresh Ubuntu 22.04 VPS running the LineUp backend.

---

## 1. Prerequisites

Install Docker CE, git, and curl:

```bash
# Update package index
apt-get update && apt-get upgrade -y

# Install curl and git
apt-get install -y curl git

# Install Docker CE (official script)
curl -fsSL https://get.docker.com | sh

# Add current user to docker group (avoids sudo on every docker command)
usermod -aG docker $USER

# Install Docker Compose plugin (bundled with Docker CE >= 20.10)
docker compose version   # verify: should print "Docker Compose version v2.x.x"
```

> **Reboot or log out/in after `usermod`** so the group change takes effect.

---

## 2. UFW Firewall

Configure network-level isolation (satisfies INV-05 OS-level guarantee):

```bash
# Set restrictive defaults
ufw default deny incoming
ufw default allow outgoing

# Allow SSH so you don't lock yourself out
ufw allow 22/tcp

# Allow Caddy — HTTP for ACME challenge, HTTPS for all real traffic
ufw allow 80/tcp
ufw allow 443/tcp

# NOTE: ports 5432 (Postgres) and 6379 (Redis) are intentionally NOT opened.
# docker-compose.prod.yml uses expose: (not ports:) for db and redis,
# so they are only reachable within the Docker bridge network.
# UFW provides the OS-level guarantee that they are unreachable from outside.

ufw --force enable
ufw status verbose   # verify: only 22, 80, 443 show ALLOW
```

---

## 3. Clone & Configure

```bash
# Clone the repository
git clone <your-repo-url> /opt/lineup
cd /opt/lineup

# Copy the env template and fill in your secrets
cp .env.prod.example .env.prod
nano .env.prod   # or use your preferred editor
```

Required secrets to fill in `.env.prod`:

| Variable | Description |
|----------|-------------|
| `SECRET_KEY` | 64-char random string (`openssl rand -hex 32`) |
| `DATABASE_URL` | `postgresql+asyncpg://lineup:PASSWORD@db:5432/lineup` |
| `REDIS_URL` | `redis://redis:6379/0` |
| `SPORTMONKS_API_TOKEN` | Your Sportmonks API key |
| `POSTGRES_PASSWORD` | Must match password in `DATABASE_URL` |

---

## 4. First-Time Deploy

```bash
cd /opt/lineup

# Run the full deploy: build images, run migrations, seed initial data
./deploy.sh --first
```

`deploy.sh --first` will:
1. Pull latest git changes
2. Build Docker images
3. Start Postgres and Redis
4. Run Alembic migrations (`alembic upgrade head`)
5. Run initial player import (`scripts/run_initial_import.py`)
6. Start all services (API, Celery worker, Celery beat, Caddy)

---

## 5. Cron Health Check

Add the health check script to crontab to run every 5 minutes:

```bash
# Make the script executable
chmod +x /opt/lineup/scripts/healthcheck_cron.sh

# Open crontab editor
crontab -e

# Add this line:
*/5 * * * * /opt/lineup/scripts/healthcheck_cron.sh >> /var/log/lineup_health.log 2>&1
```

---

## 6. Verify

```bash
# Check all containers are running
docker compose -f /opt/lineup/docker-compose.prod.yml ps

# Hit the health endpoint
curl -sf https://<your-domain>/health   # should return {"status":"ok"}

# Run the full system check script
python scripts/check_system.py --base-url https://<your-domain> --sportmonks-token <token>
# Expected: 7/7 checks PASSED
```

### Container status reference

| Service | Expected status |
|---------|----------------|
| `lineup-api` | Up (healthy) |
| `lineup-worker` | Up |
| `lineup-beat` | Up |
| `lineup-caddy` | Up |
| `lineup-db` | Up (healthy) |
| `lineup-redis` | Up (healthy) |

---

## Troubleshooting

**Caddy TLS fails (can't obtain certificate):**
- Ensure ports 80 and 443 are open in UFW and your cloud provider's security group.
- Ensure your domain's A record points to this VPS IP.

**API returns 500 on startup:**
- Check logs: `docker compose -f docker-compose.prod.yml logs api`
- Most common cause: `DATABASE_URL` in `.env.prod` is wrong or Postgres is still initialising. Wait 10s and retry.

**Celery worker not processing tasks:**
- Check: `docker compose -f docker-compose.prod.yml logs worker`
- Ensure `REDIS_URL` in `.env.prod` matches the service name `redis` (not `localhost`).
