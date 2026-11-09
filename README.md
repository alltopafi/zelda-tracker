# Zelda restock tracker

Watches product pages and pings a Discord webhook the moment something goes
from out of stock to buyable. It alerts; it does not buy. The alert links
straight to the product page so you can check out on your phone.

| Product | Retailers | How it's checked |
|---|---|---|
| Switch 2 Pro Controller, Zelda 40th | Best Buy, Target, Walmart | real Chromium tab |
| Pro Controller + display stand (Nintendo exclusive, $109.99) | Nintendo Store | HTTP |
| HORI Electric Ocarina of Time | Nintendo Store, HORI USA | HTTP |

Everything about what is tracked lives in `deploy/config.toml`.

## Deploy to k3s

**1. Build the image** (amd64 or arm64; about 2 GB because it carries Chromium):

```sh
docker build -t zelda-tracker:0.1.0 .
```

The base image tag and the Playwright pip package are pinned together through
`PLAYWRIGHT_VERSION` (default 1.63.0). If that tag fails to pull, build with
`--build-arg PLAYWRIGHT_VERSION=1.58.0`.

**2. Get the image to the cluster.** Either push it to a registry your nodes
can pull from and set `newName`/`newTag` in `deploy/kustomization.yaml`, or
import it directly on the node that will run the pod:

```sh
docker save zelda-tracker:0.1.0 | sudo k3s ctr images import -
```

**3. Create the secret** (Discord: channel settings > Integrations > Webhooks):

```sh
kubectl create namespace zelda-tracker
kubectl -n zelda-tracker create secret generic zelda-tracker \
  --from-literal=DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/...'
```

Add `--from-literal=VNC_PASSWORD='...'` if you want the browser view password-protected.

**4. Apply:**

```sh
kubectl apply -k deploy/
kubectl -n zelda-tracker logs -f deploy/zelda-tracker
```

You should get a "Restock tracker started" message in Discord within a minute.
Confirm the ping works on your phone:

```sh
kubectl -n zelda-tracker exec deploy/zelda-tracker -- python3 -m tracker test-notify --ping
```

## Day-to-day

| Task | Command (prefix with `kubectl -n zelda-tracker`) |
|---|---|
| Watch what it sees | `logs -f deploy/zelda-tracker` |
| Check the HTTP retailers right now | `exec deploy/zelda-tracker -- python3 -m tracker check-once --retailer nintendo` |
| Look at the browser | `port-forward deploy/zelda-tracker 6080:6080`, then open http://localhost:6080/vnc.html |
| Stop tabs reloading while you use the browser | `exec deploy/zelda-tracker -- python3 -m tracker pause` (then `resume`) |
| Change products, intervals, selectors | edit `deploy/config.toml`, then `apply -k deploy/` (the pod restarts itself) |

There is deliberately no Service or Ingress: the browser view is only
reachable through `port-forward`.

## What the Discord messages mean

- **IN STOCK** (pings you): a page went from not buyable to buyable. One alert
  per restock; it will not repeat while the item stays in stock.
- **"... is blocking checks"**: the retailer returned 403/429 or a challenge
  page. The tracker does not try to get past it. It backs off (up to 30
  minutes) and keeps retrying. For a browser retailer you can open noVNC and
  clear the challenge by hand; for an HTTP retailer, set `fetch = "browser"`.
- **"Can't read stock"**: the page loaded but no stock signal was found five
  times in a row, usually a page redesign. The last page is saved in
  `/data/debug/`. Fix it by setting `buy_selectors` for that retailer in
  `config.toml`, and test a saved page with
  `python3 -m tracker evaluate <retailer> <file.html>`.
- **Still watching**: daily summary, so silence never means it died.

## How stock is decided

- **Nintendo, HORI:** the page's schema.org data (`offers.availability`), then
  availability meta tags.
- **Best Buy, Target:** the rendered buy button (text, disabled state), then
  schema.org data.
- **Walmart:** the page's embedded product data, which includes the seller. A
  marketplace seller does not count as a restock.
- **Everywhere:** an "in stock" price above the product's `max_price` is
  treated as a reseller and ignored.

## Not verified against the live sites

This was built and tested without network access to the retailers. Tested:
the parsing and alert logic (72 unit tests), and the real HTTP client, Discord
client, and a real headed Chromium against a local test server. Not tested:

- The Best Buy, Target and Walmart button selectors and Walmart's embedded data
  path are best-effort and may not match today's pages. Watch the first hour of
  logs: each retailer should log `out_of_stock` with a sensible detail. If one
  logs `unknown`, adjust its selectors as described above.
- Whether Nintendo and HORI accept plain HTTP requests from your cluster. If
  either logs `blocked`, switch it to `fetch = "browser"`.
- The Docker build and the Kubernetes manifests (syntax-checked only).

## Run locally

```sh
pip install playwright && playwright install chromium
pytest
DISCORD_WEBHOOK_URL=... python3 -m tracker --config deploy/config.toml --data-dir ./data run
```

Set `TRACKER_HEADLESS=1` on a machine with no display.
