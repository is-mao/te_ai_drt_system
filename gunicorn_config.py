import multiprocessing
import os

# Bind
bind = f"0.0.0.0:{os.environ.get('PORT', '5001')}"

# Workers: 2-4x CPU cores for I/O bound apps, but cap at 4 for free tier
workers = min(multiprocessing.cpu_count() * 2, 4)

# Timeout (seconds) - generous for AI API calls
timeout = 120

# Graceful timeout
graceful_timeout = 30

# Keep-alive
keepalive = 5

# Logging
accesslog = "-"  # stdout
errorlog = "-"  # stderr
loglevel = os.environ.get("LOG_LEVEL", "info")

# Security: limit request sizes
limit_request_line = 8190
limit_request_fields = 100
limit_request_field_size = 8190
