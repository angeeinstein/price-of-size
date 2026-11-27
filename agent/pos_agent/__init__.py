"""Node agent: keeps the service (worker or coordinator) running in A/B slots,
updates it to coordinator-signed releases with health checks and rollback, and
updates itself through a short-lived helper container."""
