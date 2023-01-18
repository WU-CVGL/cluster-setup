# Prometheus

## Notes

### Bearer token

Scraping Determined-AI-master's metrics (`/prom/det-state-metrics`) with Determined-AI API needs a `bearer_token`. You can get this token by:

```bash
curl -s "https://10.0.1.66:8080/api/v1/auth/login" \ 
  -H 'Content-Type: application/json' \
  --data-binary '{"username":"admin","password":"********"}'
```

Then you can use this token in `prometheus.yaml`.

Reference: [Determined AI Docs - REST API - Authentication](https://docs.determined.ai/latest/reference/rest-api.html?highlight=api%20login#authentication)
