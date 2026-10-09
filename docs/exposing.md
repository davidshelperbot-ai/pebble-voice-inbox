# Getting the ring to reach your machine

The ring posts from your phone over the internet, so the service needs a public
HTTPS URL. It listens on `127.0.0.1` by default and should stay that way —
put something in front of it rather than binding to `0.0.0.0`.

## Tailscale Funnel (simplest)

```bash
tailscale funnel --set-path=/voice 8790
tailscale funnel status
```

That gives you `https://<machine>.<tailnet>.ts.net/voice`, with TLS handled for
you. Point the ring's webhook at `.../voice/webhook`.

Funnel strips the path prefix, so the service still sees `/webhook`.

## A custom domain in front

If you want `https://ring.example.com/webhook`, put a CDN or reverse proxy in
front of the Funnel URL. Using CloudFront as the example, two settings matter
and are easy to get wrong:

- **Forward the `Authorization` header but *not* `Host`.** Funnel routes on
  `Host`; forwarding your own domain's `Host` breaks the origin. On CloudFront
  that is the `AllViewerExceptHostHeader` origin request policy, not
  `AllViewer`.
- **Disable caching and allow POST.** A cached or rejected POST looks exactly
  like a broken ring.

The same two constraints apply to nginx, Caddy or Cloudflare: pass the auth
header through, keep the upstream's own `Host`, don't cache.

## Debugging a ring that is not working

`GET /health` needs no auth, so check reachability first:

```bash
curl https://your-host/health
```

If that works and POSTs do not, it is almost always the credential. The log
distinguishes the cases explicitly, without ever printing your token:

```
401: no Authorization header; auth-ish headers seen=['X-Auth-Token']
401 on /webhook: scheme='<none>' ... value_matches=['ring']
401 hint: a valid token was sent in the wrong header: ['Bearer=ring']
```

That last one is the most common mistake: the Pebble app has separate name and
value fields, and `Bearer` belongs at the front of the **value**, not in the
name box.
